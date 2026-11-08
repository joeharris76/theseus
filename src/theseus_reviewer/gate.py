from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._vendor import yaml
from .matchers import Matchers, load_matchers
from .policy import Binding, Consumer, Policy, PromotionRules, load_policy, parse_policy
from .review_check import (
    API_ROOT,
    ERROR,
    PASS,
    WAITING,
    CheckError,
    annotate_receipt,
    changed_paths,
    decide,
    fetch_base_change_date,
    fetch_bump_refusals,
    fetch_comments,
    fetch_files,
    fetch_head_oid,
    fetch_promotion_refusals,
    fetch_pull,
    fetch_reviews,
    fetch_statuses,
    fetch_text,
    path_matcher,
)

GATE_CONTEXT = "review-gate"
DESCRIPTION_LIMIT = 140
PENDING_DESCRIPTION = "review-gate: evaluating this head"
PULL_READ_ATTEMPTS = 3
PULL_READ_BACKOFF_SECONDS = 2
HEAD_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
WAITING_EXIT = 3
RESULT_FILE_ENV = "REVIEW_GATE_RESULT_FILE"


class UngatedBase(Exception):
    pass


class PullReadFailed(Exception):
    pass


class EvaluationFailed(Exception):
    def __init__(self, head_sha: str, cause: Exception) -> None:
        super().__init__(str(cause) or type(cause).__name__)
        self.head_sha = head_sha


def gate_state(status: int) -> str:
    return "success" if status == PASS else "failure"


def gate_description(message: str) -> str:
    return message if len(message) <= DESCRIPTION_LIMIT else message[: DESCRIPTION_LIMIT - 1] + "…"


def status_payload(status: int, message: str, target_url: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "state": gate_state(status),
        "context": GATE_CONTEXT,
        "description": gate_description(message),
    }
    if target_url is not None:
        payload["target_url"] = target_url
    return payload


def target_url(repo: str) -> str | None:
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not run_id:
        return None
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    return f"{server}/{repo}/actions/runs/{run_id}"


def pending_payload(target_url: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"state": "pending", "context": GATE_CONTEXT, "description": PENDING_DESCRIPTION}
    if target_url is not None:
        payload["target_url"] = target_url
    return payload


def post_status(token: str, repo: str, head_sha: str, status: int, message: str) -> None:
    _post_payload(token, repo, head_sha, status_payload(status, message, target_url(repo)))


def post_pending(token: str, repo: str, head_sha: str) -> None:
    _post_payload(token, repo, head_sha, pending_payload(target_url(repo)))


def _post_payload(token: str, repo: str, head_sha: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{API_ROOT}/repos/{repo}/statuses/{head_sha}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "theseus-gate",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise CheckError(f"GitHub request to post {GATE_CONTEXT} for {head_sha} failed: {exc}") from exc


@dataclass(frozen=True)
class GateConfig:
    policy: Policy
    policy_path: str
    matchers: Matchers

    @property
    def binding(self) -> Binding:
        return self.policy.binding()

    @property
    def consumer(self) -> Consumer:
        return self.policy.consumer


def load_gate_config(policy_path: str, root: Path) -> GateConfig:
    try:
        policy = load_policy(root / policy_path)
        return GateConfig(policy, policy_path, load_matchers(policy.consumer, root))
    except Exception as exc:
        raise CheckError(f"cannot load the review policy {policy_path}: {exc}") from exc


def load_promotion_policy(token: str, repo: str, base_ref: str, policy_path: str) -> PromotionRules:
    try:
        raw = fetch_text(token, repo, policy_path, base_ref)
        return parse_policy(yaml.safe_load(raw)).promotion
    except Exception as exc:
        raise CheckError(f"cannot load the base review policy: {exc}") from exc


def read_pull(repo: str, pr: int, token: str) -> tuple[dict[str, Any], str, str]:
    last: Exception | None = None
    for attempt in range(PULL_READ_ATTEMPTS):
        if attempt:
            time.sleep(PULL_READ_BACKOFF_SECONDS * attempt)
        try:
            pull = fetch_pull(token, repo, pr)
            return pull, pull["base"]["ref"], pull["head"]["sha"]
        except Exception as exc:
            last = exc
    raise PullReadFailed(f"cannot read #{pr} after {PULL_READ_ATTEMPTS} attempts: {last}") from last


def evaluate(repo: str, pr: int, token: str, config: GateConfig) -> tuple[str, int, str]:
    gated_base = config.consumer.gated_base
    pull, base, head_sha = read_pull(repo, pr, token)
    if base != gated_base:
        raise UngatedBase(f"#{pr} targets {base}, not {gated_base}; {GATE_CONTEXT} is posted only for {gated_base}")
    try:
        items = fetch_files(token, repo, pr)
        files = changed_paths(items)
        classifier = config.policy.classifier
        matcher = path_matcher(
            len(items), pull["changed_files"], config.matchers, classifier.exempt_paths, classifier.gate_paths
        )
        statuses = fetch_statuses(token, repo, head_sha)
        base_date = fetch_base_change_date(token, repo, pr)
        comments = fetch_comments(token, repo, pr)
        reviews = fetch_reviews(token, repo, pr)
        annotate_receipt(token, reviews, head_sha, config.binding.bot_login, base_date)
        bump = fetch_bump_refusals(
            token,
            repo,
            pull,
            files,
            len(items),
            comments,
            policy_path=config.policy_path,
            fixture_prefix=config.consumer.omp_fixture_prefix,
            attesters=config.binding.attesters,
        )
        from . import promotion

        promo_policy = (
            load_promotion_policy(token, repo, pull["base"].get("sha") or pull["base"]["ref"], config.policy_path)
            if (
                promotion.LOCK_PATH in files
                or promotion.has_planted_promotion_path(files)
                or len(items) < pull["changed_files"]
            )
            else PromotionRules()
        )
        is_promotion, promo = fetch_promotion_refusals(
            token,
            repo,
            pull,
            files,
            len(items),
            reviews,
            base_date,
            statuses=statuses,
            review_login=config.binding.bot_login,
            rules=promo_policy,
            comments=comments,
        )
        status, message = decide(
            head_sha,
            files,
            statuses,
            matcher,
            base_date,
            comments,
            reviews=reviews,
            binding=config.binding,
            bump_refusals=bump,
            promotion_refusals=promo,
            is_promotion=is_promotion,
        )
    except Exception as exc:
        raise EvaluationFailed(head_sha, exc) from exc
    return head_sha, status, message


def _head_sha(value: str) -> str:
    if not HEAD_SHA_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("must be 40 lowercase hex characters")
    return value


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Post the review-gate commit status for a pull request head from the same "
            "decision the shared review checker reads."
        )
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--policy", required=True, help="Repository-relative path of the consumer review policy.")
    parser.add_argument("--root", default=".", help="Trusted checkout holding the policy and manifests.")
    parser.add_argument("--no-post", action="store_true")
    parser.add_argument("--head-sha", type=_head_sha)
    parser.add_argument("--pending-first", action="store_true")
    return parser.parse_args(argv)


def _fail_closed(args: argparse.Namespace, token: str, exc: EvaluationFailed) -> int:
    print(f"review-gate: error: {exc}", file=sys.stderr)
    if args.no_post:
        return ERROR
    try:
        post_status(token, args.repo, exc.head_sha, ERROR, f"{GATE_CONTEXT}: evaluation error: {exc}")
    except CheckError as post_exc:
        print(f"review-gate: error: {post_exc}", file=sys.stderr)
    return ERROR


def _graphql_head(args: argparse.Namespace, token: str) -> str | None:
    try:
        head_sha = fetch_head_oid(token, args.repo, args.pr)
    except Exception as exc:
        print(f"review-gate: error: cannot read the head of #{args.pr} through GraphQL: {exc}", file=sys.stderr)
        return None
    if not isinstance(head_sha, str) or not HEAD_SHA_PATTERN.fullmatch(head_sha):
        print(f"review-gate: error: GraphQL returned no usable head for #{args.pr}", file=sys.stderr)
        return None
    return head_sha


def _owned_head(args: argparse.Namespace, token: str) -> str | None:
    head_sha = _graphql_head(args, token)
    if head_sha is None:
        return None
    if args.head_sha is not None and args.head_sha != head_sha:
        print(f"review-gate: error: {args.head_sha} is not the head of #{args.pr}; nothing is posted", file=sys.stderr)
        return None
    return head_sha


def _read_failed(args: argparse.Namespace, token: str, exc: Exception, known_head: str | None) -> int:
    head_sha = known_head
    if head_sha is None and not args.no_post:
        head_sha = _owned_head(args, token)
    if head_sha is None:
        print(f"review-gate: error: {exc}", file=sys.stderr)
        return ERROR
    return _fail_closed(args, token, EvaluationFailed(head_sha, exc))


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("review-gate: error: GITHUB_TOKEN is not set", file=sys.stderr)
        return ERROR
    if args.repo.count("/") != 1:
        print("review-gate: error: --repo must be OWNER/NAME", file=sys.stderr)
        return ERROR
    if args.pending_first and args.no_post:
        print("review-gate: error: --pending-first cannot be combined with --no-post", file=sys.stderr)
        return ERROR
    try:
        config = load_gate_config(args.policy, Path(args.root))
    except CheckError as exc:
        return _read_failed(args, token, exc, None)
    return post_gate(args, token, config)


def mark_pending(args: argparse.Namespace, token: str, config: GateConfig) -> None:
    try:
        _, base, head_sha = read_pull(args.repo, args.pr, token)
        if base == config.consumer.gated_base:
            post_pending(token, args.repo, head_sha)
    except (PullReadFailed, CheckError) as exc:
        print(f"review-gate: error: {exc}", file=sys.stderr)


def post_gate(args: argparse.Namespace, token: str, config: GateConfig) -> int:
    if args.pending_first:
        mark_pending(args, token, config)
    try:
        head_sha, status, message = evaluate(args.repo, args.pr, token, config)
    except UngatedBase as exc:
        print(f"review-gate: {exc}")
        return PASS
    except PullReadFailed as exc:
        return _read_failed(args, token, exc, None)
    except EvaluationFailed as exc:
        return _fail_closed(args, token, exc)
    except (CheckError, KeyError, ValueError) as exc:
        print(f"review-gate: error: {exc}", file=sys.stderr)
        return ERROR
    print(message)
    if args.no_post:
        return PASS if status == PASS else WAITING
    try:
        latest_sha, latest_status, latest_message = evaluate(args.repo, args.pr, token, config)
    except UngatedBase as exc:
        print(f"review-gate: {exc}")
        return PASS
    except PullReadFailed as exc:
        return _read_failed(args, token, exc, head_sha)
    except EvaluationFailed as exc:
        return _fail_closed(args, token, exc)
    except (CheckError, KeyError, ValueError) as exc:
        print(f"review-gate: error: {exc}", file=sys.stderr)
        return ERROR
    if (latest_sha, latest_status, latest_message) != (head_sha, status, message):
        print(latest_message)
        head_sha, status, message = latest_sha, latest_status, latest_message
    try:
        post_status(token, args.repo, head_sha, status, message)
    except CheckError as exc:
        print(f"review-gate: error: {exc}", file=sys.stderr)
        return ERROR
    return PASS if status == PASS else WAITING


def process_exit(status: int) -> int:
    return WAITING_EXIT if status == WAITING else status


def run(argv: list[str] | None = None) -> int:
    code = process_exit(main(argv))
    target = os.environ.get(RESULT_FILE_ENV)
    if target:
        Path(target).write_text(f"{code}\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(run())
