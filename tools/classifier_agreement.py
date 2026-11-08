from __future__ import annotations

import argparse
import concurrent.futures
import csv
import fnmatch
import json
import math
import os
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

THESEUS_SRC = Path(os.environ.get("THESEUS_SRC", str(Path(__file__).resolve().parents[1] / "src"))).expanduser()
sys.path.insert(0, str(THESEUS_SRC))
from theseus_reviewer import classify as C  # noqa: E402
from theseus_reviewer import decisions  # noqa: E402
from theseus_reviewer.policy import SIZE_BUCKETS  # noqa: E402

DECISIONS_URL = "https://api.openai.com/v1/decisions"
DECISIONS_MODEL = "gpt-6-luna"
JEV_URL = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/") + "/v1/systemone"
JEV_MODEL = "jev-1.14.0"
PRICE_INPUT, PRICE_CACHED, PRICE_WRITE = 0.10 / 1e6, 0.01 / 1e6, 0.125 / 1e6
BAR = C.CONFIDENCE_BAR
MAX_FILES = C.MAX_SCORED_FILES
PR_QUESTIONS = {"size": C.SIZE_QUESTION, "capability": C.CAPABILITY_QUESTION, "template": C.TEMPLATE_QUESTION}
CLASS_LINE = re.compile(
    r"Classification: size (\w+), capability (\w+), template (\w+) v\d+\s*\(confidence ([0-9.]+)\)\. "
    r"Gate signals: ([^.]*)\."
)
FILE_LINE = re.compile(r"^(.+?) \(\+(\d+)/-(\d+), ([a-z]+)\)")
PRIORITY_ROW = re.compile(r"^(.+?) \| ([0-9]+) \| (low|high)$")


def build_corpus(archive: Path, out: Path, repo: str) -> None:
    manifest = list(csv.DictReader((archive / "manifest.tsv").open(encoding="utf-8"), delimiter="\t"))
    seen: set[Any] = set()
    rows = []
    for item in manifest:
        if item["name"] != "oracle-verdict-capture":
            continue
        with zipfile.ZipFile(archive / "zips" / f"{item['artifact_id']}.zip") as zf:
            for name in zf.namelist():
                capture = json.loads(zf.read(name))
                run = capture.get("run_id")
                if run in seen:
                    continue
                text = json.dumps(capture).replace("\\n", "\n")
                match = CLASS_LINE.search(text.replace("\n", " "))
                start = text.find("\nChanged files:\n")
                if not match or start < 0:
                    continue
                block = text[start + len("\nChanged files:\n") :].split("\n\n", 1)[0]
                stats = []
                for line in block.split("\n"):
                    parsed = FILE_LINE.match(line.strip().lstrip("-* "))
                    if parsed:
                        stats.append([parsed[1], int(parsed[2]), int(parsed[3]), parsed[4]])
                if not stats:
                    continue
                priority = []
                begin = text.find("-BEGIN FILE PRIORITY\n")
                if begin >= 0:
                    for line in text[begin + 21 :].split("-END FILE PRIORITY")[0].split("\n"):
                        row = PRIORITY_ROW.match(line.strip())
                        if row:
                            priority.append([row[1], int(row[2]), row[3]])
                seen.add(run)
                rows.append(
                    {
                        "run": run,
                        "pr": capture.get("pr"),
                        "head": capture.get("head_sha"),
                        "repo": repo,
                        "created": item["created_at"],
                        "gate": match[5].strip() != "none",
                        "stats": stats,
                        "jev": {
                            "size": match[1],
                            "capability": match[2],
                            "template": match[3],
                            "confidence": float(match[4]),
                            "files": priority,
                        },
                    }
                )
    out.mkdir(parents=True, exist_ok=True)
    with (out / "corpus.jsonl").open("w", encoding="utf-8") as handle:
        for row in sorted(rows, key=lambda r: r["created"]):
            handle.write(json.dumps(row) + "\n")
    print(f"corpus: {len(rows)} runs, {len({r['pr'] for r in rows})} PRs -> {out / 'corpus.jsonl'}")


def _body(state: Any, questions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "model": DECISIONS_MODEL,
        "input": [
            {"role": "developer", "content": decisions.FIXED_DATA_NOTICE},
            {"role": "user", "content": [{"type": "input_json", "data": state}]},
        ],
        "questions": questions,
    }


def _ranked(stats: list[list[Any]]) -> list[list[Any]]:
    return sorted(stats, key=lambda s: -(s[1] + s[2]))[:MAX_FILES]


def pr_request(row: dict[str, Any]) -> dict[str, Any]:
    stats = [tuple(s) for s in row["stats"]]
    state = C.pr_state(row["repo"], row["pr"], "0" * 40, row["head"], stats, ["gate"] if row["gate"] else [])
    return _body(state, [decisions._question(qid, q) for qid, q in PR_QUESTIONS.items()])


def files_request(row: dict[str, Any]) -> dict[str, Any]:
    state, questions, _ = C.decisions_batch([tuple(s) for s in row["stats"]])
    return _body(state, [decisions._question(name, question) for name, question in questions.items()])


def plan_requests(out: Path) -> None:
    corpus = [json.loads(line) for line in (out / "corpus.jsonl").open(encoding="utf-8")]
    chars = {"pr": [], "files": []}
    with (out / "requests.jsonl").open("w", encoding="utf-8") as handle:
        for row in corpus:
            pr_body, files_body = pr_request(row), files_request(row)
            chars["pr"].append(len(json.dumps(pr_body)))
            chars["files"].append(len(json.dumps(files_body)))
            handle.write(json.dumps({"run": row["run"], "pr": pr_body, "files": files_body}) + "\n")
    est = {k: [c / 4 for c in v] for k, v in chars.items()}
    per_run = [a + b for a, b in zip(est["pr"], est["files"])]
    print(f"requests: {len(corpus)} runs x 2 requests -> {out / 'requests.jsonl'}  (no API call made)")
    for k, v in est.items():
        print(
            f"  {k:5s} est. tokens/request: mean {statistics.mean(v):.0f}, p90 {sorted(v)[int(0.9 * len(v))]:.0f}, max {max(v):.0f}"
        )
    print(
        f"  est. cost per run (both requests): mean ${statistics.mean(per_run) * PRICE_INPUT:.6f}, "
        f"max ${max(per_run) * PRICE_INPUT:.6f}; whole corpus x1 ${sum(per_run) * PRICE_INPUT:.4f}  [ESTIMATE: chars/4]"
    )


class Failed(Exception):
    pass


def _post(url: str, key: str, body: dict[str, Any], timeout: float) -> tuple[dict[str, Any], float]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST", headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise Failed(f"http {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise Failed(f"unreachable: {type(exc).__name__}") from exc
    try:
        return json.loads(raw), time.monotonic() - started
    except ValueError as exc:
        raise Failed("non-JSON body") from exc


def run_live(args: argparse.Namespace, out: Path) -> None:
    if not (args.live and args.owner_approved):
        raise SystemExit("refusing: live calls need --live --owner-approved (owner approval is required first)")
    key = os.environ.get("OPENAI_API_KEY", "")
    jev_key = os.environ.get("TYPESAFE_API_KEY", "") if args.jev_replay else ""
    if not key or (args.jev_replay and not jev_key):
        raise SystemExit("OPENAI_API_KEY (and TYPESAFE_API_KEY with --jev-replay) must be set")
    requests = [json.loads(line) for line in (out / "requests.jsonl").open(encoding="utf-8")][: args.limit or None]
    corpus = {r["run"]: r for r in map(json.loads, (out / "corpus.jsonl").open(encoding="utf-8"))}
    target = out / "responses.jsonl"
    done = {(r["run"], r["repeat"], r["arm"]) for r in map(json.loads, target.open())} if target.exists() else set()
    lock = threading.Lock()

    def job(item: dict[str, Any], repeat: int, arm: str) -> None:
        if (item["run"], repeat, arm) in done:
            return
        record: dict[str, Any] = {"run": item["run"], "repeat": repeat, "arm": arm}
        try:
            if arm == "jev":
                row = corpus[item["run"]]
                stats = [tuple(s) for s in row["stats"]]
                state = C.pr_state(
                    row["repo"], row["pr"], "0" * 40, row["head"], stats, ["gate"] if row["gate"] else []
                )
                payload, secs = _post(
                    JEV_URL, jev_key, {"model": JEV_MODEL, "state": state, "questions": PR_QUESTIONS}, 60
                )
                record.update(raw=payload.get("answers"), secs=secs)
            else:
                payload, secs = _post(DECISIONS_URL, key, item[arm], args.timeout)
                record.update(raw=payload, secs=secs)
        except Failed as exc:
            record["error"] = str(exc)
        with lock, target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    arms = ["pr", "files"] + (["jev"] if args.jev_replay else [])
    with concurrent.futures.ThreadPoolExecutor(args.concurrency) as pool:
        futures = [pool.submit(job, item, rep, arm) for item in requests for rep in range(args.repeats) for arm in arms]
        for count, future in enumerate(concurrent.futures.as_completed(futures), 1):
            future.result()
            if count % 100 == 0:
                print(f"  {count}/{len(futures)}")
    print(f"responses -> {target}")


def to_jev_shape(answer: dict[str, Any], q: dict[str, Any], name: str) -> dict[str, Any]:
    if not isinstance(answer, dict) or answer.get("name") != name:
        raise Failed("answer name/order mismatch")
    kind = answer.get("type")
    if kind == "refusal":
        raise Failed("refusal")
    if kind != q["type"]:
        raise Failed("answer type mismatch")

    def number(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise Failed("non-numeric probability")
        if not 0.0 <= value <= 1.0 + 1e-9:
            raise Failed("probability out of range")
        return float(value)

    if kind == "choice":
        labels = list(q["criteria"])
        probs = {item.get("value"): number(item.get("probability")) for item in answer.get("probabilities") or []}
        if set(probs) != set(labels) or answer.get("choice") not in labels:
            raise Failed("choice labels mismatch")
        if abs(sum(probs.values()) - 1.0) > 0.02:
            raise Failed("probabilities do not sum to 1")
        return {"choice": answer["choice"], "probabilities": probs, "confidence": number(answer.get("confidence"))}
    levels = len(q["criteria"])
    probs = {}
    for item in answer.get("probabilities") or []:
        value = item.get("value")
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < levels:
            raise Failed("score level out of range")
        probs[str(value)] = number(item.get("probability"))
    score = answer.get("score")
    if len(probs) != levels or abs(sum(probs.values()) - 1.0) > 0.02:
        raise Failed("score probabilities malformed")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= levels - 1:
        raise Failed("score out of range")
    return {"score": float(score), "probabilities": probs, "confidence": number(answer.get("confidence"))}


def parse_decision(payload: dict[str, Any], body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    answers = payload.get("answers")
    questions = body["questions"]
    if not isinstance(answers, list) or len(answers) != len(questions):
        raise Failed("answer count mismatch")
    out = {}
    for q, a in zip(questions, answers):
        source = PR_QUESTIONS.get(q["name"]) or (
            C.FILE_IMPACT_QUESTION if q["name"].startswith("impact.") else C.FILE_SCORE_QUESTION
        )
        out[q["name"]] = to_jev_shape(a, source, q["name"])
    return out


def idx(bucket: str) -> int:
    return SIZE_BUCKETS.index(bucket)


def decide(
    answers: dict[str, Any], gate: bool, size_mode: str, template_mode: str, capability_mode: str
) -> tuple[str, str, str]:
    size = answers["size"]
    p = size["probabilities"].get(size["choice"], 0.0)
    i = idx(size["choice"])
    if p < BAR and size_mode == "down":
        i = max(i - 1, idx("small"))
    elif p < BAR and size_mode == "up":
        i = min(i + 1, len(SIZE_BUCKETS) - 1)
    if SIZE_BUCKETS[i] == "trivial" and size["probabilities"].get("trivial", 0.0) < BAR:
        i = idx("small")
    cap = answers["capability"]
    capability = cap["choice"]
    if cap["probabilities"].get(capability, 0.0) < BAR:
        capability = "routine" if capability_mode == "down" else "deep"
    tpl = answers["template"]
    template = tpl["choice"]
    if gate:
        template, i = "gate", max(i, idx(C.GATE_BUCKET_FLOOR))
    elif tpl["probabilities"].get(template, 0.0) < BAR:
        template = {"down": "standard", "keep": template, "up": "deep" if template == "standard" else template}[
            template_mode
        ]
    return SIZE_BUCKETS[i], capability, template


def size_floor(row: dict[str, Any], lines: int, data_globs: list[str]) -> str:
    counted = sum(a + d for p, a, d, _ in row["stats"] if not any(fnmatch.fnmatch(p, g) for g in data_globs))
    return "large" if lines and counted >= lines else "trivial"


VARIANTS = {
    "jev-rules-v0.2.3": ("down", "down", "down", False),
    "owner-2026-11-07": ("down", "keep", "down", True),
    "neutral+floors": ("neutral", "keep", "up", True),
    "up+floors (planned default)": ("up", "up", "up", True),
}


def score(args: argparse.Namespace, out: Path) -> int:
    corpus = {r["run"]: r for r in map(json.loads, (out / "corpus.jsonl").open(encoding="utf-8"))}
    bodies = {r["run"]: r for r in map(json.loads, (out / "requests.jsonl").open(encoding="utf-8"))}
    responses = [json.loads(line) for line in (out / "responses.jsonl").open(encoding="utf-8")]
    by_key: dict[tuple[Any, int], dict[str, Any]] = {}
    failures: Counter[str] = Counter()
    secs, tokens = [], []
    for r in responses:
        cell = by_key.setdefault((r["run"], r["repeat"]), {})
        if "error" in r:
            failures[f"{r['arm']}: {r['error']}"] += 1
            cell[r["arm"]] = None
            continue
        if r["arm"] == "jev":
            cell["jev"] = r["raw"]
            continue
        try:
            cell[r["arm"]] = parse_decision(r["raw"], bodies[r["run"]][r["arm"]])
            secs.append(r["secs"])
            usage = r["raw"].get("usage") or {}
            details = usage.get("input_tokens_details") or {}
            cached, written = details.get("cached_tokens", 0), details.get("cache_write_tokens", 0)
            tokens.append((usage.get("input_tokens", 0) - cached - written, cached, written))
        except Failed as exc:
            failures[f"{r['arm']}: {exc}"] += 1
            cell[r["arm"]] = None
    total = len(by_key)
    failed = sum(1 for c in by_key.values() if c.get("pr") is None)
    print(
        f"responses: {total} run x repeat; PR-level failures {failed} ({failed / max(total, 1):.1%}); {dict(failures)}"
    )
    if secs:
        q = sorted(secs)
        print(f"latency: p50 {q[len(q) // 2]:.2f}s p95 {q[int(0.95 * len(q))]:.2f}s (measured)")
    if tokens:
        cost = sum(u * PRICE_INPUT + c * PRICE_CACHED + w * PRICE_WRITE for u, c, w in tokens)
        print(f"cost: ${cost:.4f} measured over {len(tokens)} requests; ${cost / max(total, 1):.6f} per run")
    verdict_ok = True
    for name, (size_mode, template_mode, capability_mode, floors) in VARIANTS.items():
        under = under2 = over = exact = tmpl_under = n = gate_ok = gate_n = 0
        flips: Counter[Any] = Counter()
        per_run: dict[Any, set[str]] = {}
        for (run, rep), cell in by_key.items():
            if cell.get("pr") is None:
                continue
            row = corpus[run]
            size, _, template = decide(cell["pr"], row["gate"], size_mode, template_mode, capability_mode)
            jev = row["jev"]["size"]
            if floors:
                floor = size_floor(row, args.size_floor_lines, args.data_glob)
                size = SIZE_BUCKETS[max(idx(size), idx(floor), idx(args.min_bucket))]
                jev = SIZE_BUCKETS[max(idx(jev), idx(floor))]
            delta = idx(size) - idx(jev)
            n += 1
            exact += delta == 0
            under += delta < 0
            under2 += delta <= -2
            over += delta > 0
            if not row["gate"] and row["jev"]["template"] in ("deep", "gate") and template == "standard":
                tmpl_under += 1
            if row["gate"]:
                gate_n += 1
                gate_ok += idx(size) >= idx("large") and template == "gate"
            per_run.setdefault(run, set()).add(size)
        flips.update(len(v) > 1 for v in per_run.values())
        if not n:
            continue
        print(
            f"\n[{name}] n={n}: exact {exact / n:.1%}, under {under / n:.1%}, under-by-2+ {under2 / n:.1%}, "
            f"over {over / n:.1%}, template-under {tmpl_under / n:.1%}, gate ok {gate_ok}/{gate_n}, "
            f"runs whose size flipped across repeats {flips[True]}/{len(per_run)}"
        )
        if name.startswith(args.accept_variant):
            ok = (
                under / n <= args.max_under
                and under2 == 0
                and tmpl_under / n <= args.max_template_under
                and failed / max(total, 1) <= args.max_failures
                and gate_ok == gate_n
            )
            verdict_ok = ok
            print(f"  ACCEPTANCE ({args.accept_variant}): {'PASS' if ok else 'FAIL'}")
    impact_agree = impact_n = 0
    score_pairs: list[tuple[int, int]] = []
    for (run, _), cell in by_key.items():
        files = cell.get("files")
        if not files:
            continue
        ranked = _ranked(corpus[run]["stats"])
        jev_rows = {p: (s, imp) for p, s, imp in corpus[run]["jev"]["files"]}
        for i, (path, *_rest) in enumerate(ranked, 1):
            if path in jev_rows:
                impact_n += 1
                impact_agree += files[f"impact.{i}"]["choice"] == jev_rows[path][1]
                score_pairs.append((jev_rows[path][0], min(5, max(1, round(files[f"importance.{i}"]["score"]) + 1))))
    if impact_n:
        print(f"\nper-file: impact agreement {impact_agree / impact_n:.1%} (n={impact_n})")
        print(f"  (Jev rendered score, Decisions level+1) pairs: {Counter(score_pairs).most_common(10)}")
        print(
            "  note: Theseus renders Jev's 0-based score with round(score) clamped to 1..5; the archive holds only 1-3."
        )
    if any(c.get("jev") for c in by_key.values()):
        print(
            "\njev-replay arm present: compare raw size choices and P(choice) directly; see the 'jev' rows in responses.jsonl"
        )
    return 0 if verdict_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agreement study of the Decisions fallback against archived TypeSafe classifications"
    )
    parser.add_argument("stage", choices=("corpus", "plan", "run", "score"))
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repo", default="")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--owner-approved", action="store_true")
    parser.add_argument("--jev-replay", action="store_true")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--size-floor-lines", type=int, default=1500)
    parser.add_argument("--data-glob", action="append", default=[])
    parser.add_argument("--min-bucket", default="trivial", choices=SIZE_BUCKETS)
    parser.add_argument("--accept-variant", default="owner-2026-11-07")
    parser.add_argument("--max-under", type=float, default=0.05)
    parser.add_argument("--max-template-under", type=float, default=0.05)
    parser.add_argument("--max-failures", type=float, default=0.01)
    args = parser.parse_args()
    if args.stage == "corpus":
        if args.archive is None or not args.repo:
            raise SystemExit("corpus needs --archive and --repo")
        build_corpus(args.archive, args.out, args.repo)
    elif args.stage == "plan":
        plan_requests(args.out)
    elif args.stage == "run":
        run_live(args, args.out)
    else:
        return score(args, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
