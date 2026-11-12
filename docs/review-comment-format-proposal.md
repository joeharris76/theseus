# Theseus reviewer output: format audit and redesign proposal

Read-only research. No edits, comments, or model calls were made. Date of evidence: 2026-11-10.

## 1. Summary

The owner's "wall of text" complaint is correct and measurable.

| Real example | Today (visible) | Proposed (visible by default) | Change |
|---|---|---|---|
| SHIP WITH FIXES, 6 findings ([BenchBox #4062](https://github.com/BenchBox-dev/benchbox/pull/4062#pullrequestreview-5827244266)) | 6,001 chars, 787 words, one 5,171-char block | 861 chars, 134 words, 9 lines | -86% |
| SHIP WITH FIXES, 1 finding ([Theseus #45](https://github.com/joeharris76/theseus/pull/45#pullrequestreview-5818828879)) | 2,793 chars, 421 words | 505 chars, 73 words, 5 lines | -82% |
| SHIP ([Theseus #45](https://github.com/joeharris76/theseus/pull/45#pullrequestreview-5819211684)) | 2,891 chars, 434 words | 224 chars, 27 words | -92% |
| Not reviewed ([BenchBox #4067](https://github.com/BenchBox-dev/benchbox/pull/4067#pullrequestreview-5830092015)) | 573 chars, posted twice on one head | 197 chars, posted once | -66% |

Nothing is deleted in the proposal: the detail moves into `<details>` blocks, so raw body size is about the same (6,071 vs 6,001 chars for #4062).

The six worst problems today:

1. **The verdict is not first.** Line 1 is `### review-verdict: failure for <40-hex sha>`. The decision word appears at character 164, after a boilerplate sentence.
2. **A SHIP is the longest thing the bot says.** The reviewer's free-text summary is 84–91% of every SHIP body sampled (1,939–2,517 chars of unbroken prose).
3. **Finding IDs are invisible where they are needed.** A thread starts `**Low**: First-round demotion…`; its `D4` id exists only in a hidden marker. The summary then says "Verified fixed: D11, D14. Resolve their review threads", and the dispute and adjudication grammars both require `D<n>`.
4. **Severity has no visual weight.** It is a bold word inside a bullet. There are no counts by severity anywhere, in the body or the status.
5. **"Not reviewed" comments leak raw provider errors and repeat.** A public PR shows `429 Cloud Code Assist API error (429): { "error": { "code": 429, "message": "Individual quota reached. Please upgrade your subscription…`, cut mid-JSON, with no next step.
6. **Status descriptions truncate the actionable part.** Gate statuses spend 40 characters on a full SHA and get cut at 140: `…which a stand-in cannot waive: fix th…`.

The biggest constraint on any redesign: **prose is currently the machine API in eight places**, two of them in BenchBox. One of those fails open (section 6). This is therefore not a Theseus-only change.

Recommended order (detail in section 9):

1. Make parsers read both old and new formats (Theseus and BenchBox), before any output changes.
2. Rewrite status descriptions (strings only, parser tokens preserved).
3. Verdict-first body, collapsed reviewer notes, severity marks and counts, visible finding IDs.
4. Humanise and de-duplicate "not reviewed" output.
5. Minimise superseded reviews as outdated.
6. Later: structured findings, a sticky dashboard comment, suggestion blocks, check-run mirror.

## 2. Evidence base and caveats

- **Code audited:** `joeharris76/theseus` at `origin/main` = `360cead` (v0.2.8). All Theseus `file:line` references below are for that commit, read with `git show origin/main:…`.
- **An older local checkout differs.** A checkout at `main@8635dc7` (v0.2.4), 62 commits behind, has different line numbers (for example `_header` is `report.py:196` there, `:252` upstream).
- **BenchBox** is pinned to Theseus v0.2.6 (`.github/theseus-lock.json`, `source_commit 2900aa6`).
- **Real output sampled:** 29 bot reviews and 18 inline threads from Theseus #45, #54 and BenchBox #4062, #4067, #4068, plus commit statuses on 7 heads.
- **Rendering was verified, not assumed.** Mockups were rendered through GitHub's `POST /markdown` API (`mode=gfm`, repository context). That endpoint may differ slightly from the live comment pipeline.
- **Not verified:** email clients, GitHub Mobile, and screen-reader behaviour. Claims about those are marked `[INFERENCE]`.

## 3. GitHub capabilities

### 3.1 Formatting features

| Feature | What the docs say | Verified by rendering | Use in Theseus? |
|---|---|---|---|
| Headings | `#` to `######`; two or more headings create an outline ([basic syntax](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax)) | Yes | One H3 per comment, carrying the verdict |
| `<details>/<summary>` | Collapsed until clicked; markdown allowed inside; `open` attribute expands by default ([collapsed sections](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/organizing-information-with-collapsed-sections)) | Yes, including inside list items and with `<b>`/`<code>` in the summary | Yes, for all secondary detail |
| Alerts `> [!NOTE]` etc. | Five types; "limit them to one or two per article"; "cannot be nested within other elements" ([basic syntax, Alerts](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax#alerts)) | Top level renders with an icon and a fixed title word. Inside `<details>` or a list item it degrades to a plain blockquote showing literal `[!WARNING]` | No (see 3.3) |
| Tables | GFM extension ([GFM spec](https://github.github.com/gfm/)) | Yes; wrapped in an accessible `role="table"` element | Only for small, short-cell tables |
| Task lists | `- [ ]` / `- [x]` ([basic syntax, Task lists](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax#task-lists)) | Yes, with `aria-label` on the checkbox | **Never** in a verdict review (see 3.4) |
| Suggestion blocks | A reviewer can "suggest a specific change to the line or lines"; the author applies one or batches several into a single commit, and the suggester becomes co-author ([commenting](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/commenting-on-a-pull-request), [incorporating feedback](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/incorporating-feedback-in-your-pull-request)) | Outside a diff-line review comment it renders as a plain code block | Later, opt-in only |
| Emoji | `:EMOJICODE:` shortcodes ([basic syntax, Using emojis](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax#using-emojis)) | `:red_circle:` becomes 🔴 only when GitHub renders it | Use Unicode characters, not shortcodes (see 3.3) |
| Permalinks | A blob URL with a commit SHA and `#L10-L20` renders as an embedded snippet "only in the repository it originated in" and "only in comments" ([permalinks](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/creating-a-permanent-link-to-a-code-snippet)) | Yes | Useful for findings that have no thread |
| SHA autolinks | n/a | A bare 40-hex SHA renders as a 7-character commit link | Yes: keeps the full SHA in the source and the display short |
| Mermaid | Supported in issues, discussions, pull requests, wikis and markdown files ([diagrams](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/creating-diagrams)) | Not tested | No; adds nothing to a verdict |
| Hidden HTML comments | "Hide content from the rendered Markdown by placing the content in an HTML comment" ([basic syntax](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax#hiding-content-with-comments)) | Stripped from both `body_html` and `body_text`; present in raw `body` | Yes; this is the right place for machine data |
| HTML subset | GFM filters `<title> <textarea> <style> <xmp> <iframe> <noembed> <noframes> <script> <plaintext>`, and GitHub applies "additional post-processing and sanitization" ([GFM spec 6.11](https://github.github.com/gfm/#disallowed-raw-html-extension-)) | `style=` attributes are stripped; `<sub>`, `<kbd>`, `<details>`, `<picture>` survive; external images are proxied | No inline colour is possible |

### 3.2 Limits and surfaces

| Item | Fact | Source |
|---|---|---|
| Commit status description | Docs say only "A short description of the status". The 140-character limit is **not in the docs page**; it is an API validation error reported by integrators ([example](https://github.com/zalando/zappr/issues/378)). Theseus encodes it at `report.py:36`, `gate.py`, `sweep.py`. | [statuses API](https://docs.github.com/en/rest/commits/statuses) |
| Statuses per commit | "A limit of 1000 statuses per sha and context"; more is a validation error. | same |
| Comment body | 65,536 characters. Also not on a docs page; reported as "Body is too long (maximum is 65536 characters)" ([example](https://github.com/telia-oss/github-pr-resource/issues/259)). Theseus encodes it at `report.py:37`. | secondary |
| Check-run output | `summary` and `text`: markdown, 65,535 chars each. Annotations: 50 per request, levels `notice`/`warning`/`failure`, title 255 chars, message 64 KB. Only GitHub Apps can create check runs. | [check runs API](https://docs.github.com/en/rest/checks/runs) |
| Notifications | Creating a review or a review comment "triggers notifications". "Batching your comments avoids sending multiple notifications." Editing a comment notifies only newly mentioned users. | [reviews API](https://docs.github.com/en/rest/pulls/reviews), [commenting](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/commenting-on-a-pull-request), [basic syntax](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax#mentioning-people-and-teams) |
| Review comment targets | The standalone comment endpoint accepts `subject_type: line` or `file`. The `comments[]` array of "create a review" lists only `path`, `position`, `body`, `line`, `side`, `start_line`, `start_side`. | [review comments API](https://docs.github.com/en/rest/pulls/comments), [reviews API](https://docs.github.com/en/rest/pulls/reviews) |
| Minimising | `PullRequestReview`, `PullRequestReviewComment` and `IssueComment` all implement `Minimizable`. Classifiers include `OUTDATED` and `RESOLVED`. | Live GraphQL schema introspection |

### 3.3 Accessibility and severity marks

- **Never colour alone.** Every mark is followed by its word in bold. Counts are stated in text. Lists are sorted most severe first.
- **Use Unicode emoji, not shortcodes.** Shortcodes are literal text in status descriptions, `body_text`, the local CLI, and BenchBox's digest. Unicode is the same bytes everywhere.
- **One emoji per line.** Screen readers read emoji names aloud ("large orange circle"). `[INFERENCE]`
- **No image badges.** Copilot uses hosted severity images with `alt="High severity"` and light/dark `<picture>` sources, but only GitHub can host those on its own domain. A third-party badge needs an external host, is proxied, and may be blocked in email. Theseus also strips URLs from model text (`verdict.py:185-186`).
- **No alerts.** Their title is a fixed word ("Caution", "Warning") that competes with the verdict vocabulary. They do not render when nested (verified). Their email and mobile rendering is unverified. A heading with a mark and a word works on every surface, including plain text.
- **Dark mode.** Emoji are colour glyphs and do not change with theme. ⚪ has low contrast on light themes, so it is used only with its word.
- **Old platforms.** 🟠 and 🟡 are Unicode 12 (2019). Where the glyph is missing, the word still carries the meaning.

### 3.4 Two traps specific to Theseus

- **A checkbox tick is an edit.** Ticking a task-list item rewrites the comment body. Theseus treats any edited bot review as tampering and holds the PR (`protocol.py:396-402`). A maintainer ticking a box in a verdict review would freeze the pull request. `[INFERENCE: ticking sets lastEditedAt; this is how task lists are implemented, but it was not tested.]`
- **Email flattens collapsibles.** Most mail clients show `<details>` content expanded. `[INFERENCE]` So the order of content matters as much as collapsing: verdict first, action second, detail last.

## 4. Prior art

Sampled live from public pull requests on 2026-11-10 (raw comment bodies via the API), plus vendor docs and source.

| Tool | Top-level summary | Inline finding | Severity display | Collapsing | Sticky edit? |
|---|---|---|---|---|---|
| GitHub Copilot code review ([docs](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/use-code-review), [example](https://github.com/mutablelogic/go-llm/pull/275#pullrequestreview-5842853775)) | One review per request. H3 verdict: `### 🟢 Approval recommended` or `### 🟡 Changes recommended`, then one sentence. | Plain paragraph (465 chars in the sample) | "High", "Medium", "Low" as text-badge images with alt text | `<details open>` "1 open issue" with linked titles; `<details>` "What changed" holds the file table | No |
| CodeRabbit ([findings](https://docs.coderabbit.ai/change-stack/findings), [walkthrough](https://docs.coderabbit.ai/pr-reviews/walkthroughs), [example](https://github.com/wundergraph/cosmo/pull/3377#discussion_r4316386651)) | Walkthrough comment, "wrapped in a collapsible `<details>` block" by default. Review body: `**Actionable comments posted: 2**`. | Label row, bold one-sentence title, two short paragraphs | `**🟠 Major**` beside category and effort labels. Levels: critical, major, minor, trivial | Tool output and "Prompt for AI Agents" collapsed | Yes; the walkthrough is edited in place |
| Semgrep ([example](https://github.com/getsentry/warden/pull/568#discussion_r4499508098)) | Short issue comment | `**🟠 High severity and reachable issue identified in your code:**` then one line | Colour circle plus word | "Why this is reachable" collapsed | No |
| Sentry ([example](https://github.com/gitroomhq/postiz-app/pull/2214#discussion_r4201257856)) | None | `**Bug:** <one sentence>` then `<sub>Severity: HIGH</sub>` | Text only | "Suggested Fix" collapsed | The sampled inline comment opens with "Resolved in `<sha>`", so it appears to be edited after the fix |
| Cursor Bugbot ([example](https://github.com/TrogonStack/trogonai/pull/774#discussion_r4519652341)) | Review body | `### <title>`, `**High Severity**`, one paragraph | Text only | None | No |
| Graphite ([example](https://github.com/mento-review-bench/shop-api/pull/16#discussion_r4525704267)) | None | Prose, a code block, then a ` ```suggestion ` block | None | None | No |
| CodeQL ([example](https://github.com/mazoomazoo/VPN-UI-UniQue/pull/1#discussion_r4500994348)) | None | `## CodeQL / <rule name>` then the message | None in body | None | No |
| SonarQube Cloud ([example](https://github.com/nymtech/nym-vpn-client/pull/7021#issuecomment-6769204120)) | One comment: `## … **Quality Gate passed**`, then five metric lines | None | Pass/fail icon images | None | Not observed in the sample |
| Codecov ([example](https://github.com/sunchayn/nimbus/pull/180#issuecomment-6757365251)) | One comment: verdict line, small table | None | `:x:` and `:warning:` | "Additional details and impacted files" collapsed | Yes (created 02:35, updated 12:36) |
| Danger ([template source](https://github.com/danger/danger-js/blob/main/source/runner/templates/githubIssueTemplate.ts)) | One comment with Fails, Warnings and Messages tables; a hidden HTML comment carries the Danger ID | `- :warning: message` | Emoji per row | None | Yes; "generated / updated by danger" |
| reviewdog ([README](https://github.com/reviewdog/reviewdog#reporters)) | Check run, or none | Review comment (`github-pr-review`) or check annotation (`github-pr-check`) | `info` / `warning` / `error` levels | None | n/a |

What the field agrees on:

1. **Verdict or severity is the first thing on the first line.** No sampled tool leads with a state name and a commit hash.
2. **A colour mark plus the severity word** is the convention (Semgrep, CodeRabbit), or the word alone (Sentry, Cursor). Nobody uses colour alone.
3. **Collapse the secondary, show the issue.** Tool logs, AI prompts, file tables and reachability essays are collapsed. The one-sentence problem statement never is.
4. **Per-PR state lives in one edited comment** (Codecov, CodeRabbit, Danger). Per-review output is a fresh review (Copilot).
5. **Operational failures do not get their own timeline entries.** CodeRabbit puts "review status messages" inside the walkthrough comment.
6. **Suggestion blocks are used "where possible"** (Copilot docs, Graphite sample), not on every finding.

Twelve tools were examined. The last four added no new pattern, so the search was stopped. Only reviewdog uses check annotations, and it is a linter bridge rather than a reviewer.

## 5. Current-state audit

### 5.1 Inventory of everything Theseus posts

Theseus `origin/main` (`360cead`). All paths are under `src/theseus_reviewer/`.

**Bodies**

| # | Output | Where rendered | Posted as |
|---|---|---|---|
| B1 | Verdict (SHIP, SHIP WITH FIXES, DO NOT SHIP) | `report.py:496-694`; render closure `:577-660` | PR review, or issue comment when `findings_delivery: comment` (`cli.py:1974-1978`, `driver.py:395-399`) |
| B2 | Header and "supersedes" line | `report.py:252-260` | Part of every body |
| B3 | Reviewer line | `report.py:263-269` | Part of B1 |
| B4 | Reviewer summary and fixed/withdrawn notes | `report.py:444-458` | Part of B1 |
| B5 | Finding bullets | `report.py:84-88`, `:137-139` | Part of B1 |
| B6 | Inline finding thread | `report.py:334-347` | Review comment |
| B7 | "Reviewers not available" | `report.py:317-331`, section at `:655` | Part of B1, or the whole body when pending |
| B8 | "Provider availability" | `report.py:280-301`, section at `:656` | Same |
| B9 | Dispute dispositions, escalation note | `report.py:474-493`, `:598-603` | Part of B1 |
| B10 | Step-back note | `report.py:54-58`, `:594-595` | Part of B1 |
| B11 | Result withheld (integrity) | `report.py:507-516` | Review or comment |
| B12 | Oversize and marker-limit notes | `report.py:41-52` | Part of B1 |
| B13 | Carried verdict | `report.py:697-737` | Review |
| B14 | Owner adjudication | `report.py:740-792` | Review |
| B15 | Repost | `report.py:795-812` | Issue comment |
| B16 | Refused | `report.py:815-844` | Review |
| B17 | Size-limit notices | `report.py:38`, `:142-147`, `:176-181` | Part of B1 |

**Status descriptions**

| # | Context | Where | Example today |
|---|---|---|---|
| S1 | `review-verdict` verdict | `report.py:425-441` | `huge: SHIP WITH FIXES, 7 defect(s) to fix` |
| S2 | `review-verdict` pending | `report.py:435-438` | `pending: large: absent (quota: 429 Cloud Code Assist API error (429): { "error": …` |
| S3 | carried, adjudicated, reposted, refused | `report.py:704`, `:791`, `:811`, `:843` | `carried from 8f192e3e1e59: SHIP` |
| S4 | exempt, fork, hold, install failure | `cli.py:1902-1906`, `report.py:851-852` | `only exempt paths changed` |
| S5 | Sweep failure surface | `sweep.py:109-114` | `review-verdict failed twice without a result at: …` |
| S6 | `review-gate` | `review_check.py:249-310`, `gate.py:44`, `:301` | `review: waiting for the review's verdict on <40 hex>, or a stand-in attestation 'Stand-in review: APPROVE 7…` |

### 5.2 Representative real output

A verdict body ([Theseus #45](https://github.com/joeharris76/theseus/pull/45#pullrequestreview-5818828879), 2,793 visible chars; summary abridged here):

```text
### review-verdict: failure for `2aff04c7ce9b8fc267aeeed71661ae03cd069a6a`

This result supersedes those for earlier heads; their open review threads still apply.

Decision: **SHIP WITH FIXES**.

Reviewer: huge (role review, omp @plan).

One fix is needed before merge. The fix for D1 still lets a finding go advisory when its path is a different
spelling of a changed file's path. Bypass scenarios I checked and ruled out:
(1) Step-back round: it widens the scope to the whole change (protocol.py:653-668), keeps every prior id, …
[… 1,776 characters, eight numbered scenarios in one paragraph …]

Verified fixed: D1. Resolve their review threads.

Scope: files changed since head `8f192e3e1e596e460c67c8103a6685ee6f504735` was reviewed.

**Reviewers not available**

- large (attempt 1): absent (incomplete): the verdict would ship but 86 of 146 changed hunks in 13 required files were not seen: tests/fixtures/review_golden/dispute_round.json (.review-pull-request.di…
- large (attempt 2): absent (incomplete): the verdict would ship but 1 of 146 changed hunks in 1 required files …
- huge (attempt 3): absent (incomplete): the verdict would ship but 86 of 146 changed hunks in 13 required files …
```

A "not reviewed" body ([BenchBox #4067](https://github.com/BenchBox-dev/benchbox/pull/4067#pullrequestreview-5830092015), posted again unchanged 75 minutes later):

```text
### review-verdict: pending for `40a8b14def5595cc5d839608c46da7916ae01bd7`

This result supersedes those for earlier heads; their open review threads still apply.

**Reviewers not available**

- large (attempt 1): absent (quota): 429 Cloud Code Assist API error (429): { "error": { "code": 429, "message": "Individual quota reached. Please upgrade your subscription to increase your limits…
- huge: skipped: claude pool blocked in this run, google-antigravity returned a quota error
- max: skipped: claude pool blocked in this run, google-antigravity returned a quota error
```

An inline thread ([BenchBox #4062](https://github.com/BenchBox-dev/benchbox/pull/4062#discussion_r4513792000), 827 visible chars; the body is a single 753-character paragraph):

```text
**High**: Migrate BenchBox's required status before retiring oracle-gate

This pin switches the producer to a release that no longer posts oracle-gate. BenchBox's current repository
contract still requires both oracle-gate and review-gate in docs/internal/repo-admin-settings.md:60-65 and
174-175; _project/scripts/ruleset_review_enforcement.py:11 and 58-59 still insists on oracle-gate, and […]
Complete the ruleset, enforcement-script, caller and documentation cutover before installing this pin.

<!-- review-defect: c1-D1 -->

<!-- review-finding: f9318c216fdba137 -->
```

A finding with no thread, inside the review body ([BenchBox #4062](https://github.com/BenchBox-dev/benchbox/pull/4062#pullrequestreview-5827244266); six of these in a row, 560–790 characters each):

```text
- **High** `.review-promotion/new/src/theseus_reviewer/gate.py:204-206`: D2: Detect shared-head collisions independently of the local base policy
  The collision check runs only when this branch's policy lists several bases, and _rival_bases additionally ignores […745 characters…]
```

### 5.3 Measurements

| Measure | Value |
|---|---|
| Characters before the decision word | 164 in every verdict body |
| Reviewer summary as share of a SHIP body | 84–91% in all five SHIP reviews sampled (1,939–2,517 of 2,169–2,891 chars) |
| Theseus #45 total | 13 bot reviews, 24,438 visible chars, 3,603 words, plus 13 threads |
| Theseus #54 total | 8 bot reviews, 17,535 visible chars, 2,461 words |
| "Supersedes" boilerplate in #45 | 13 copies, 1,118 chars |
| Inline thread length in #45 | average 1,174 chars, maximum 1,464, each one paragraph |
| Consecutive "not reviewed" reviews | 3 in a row on #45; 2 byte-identical on one head on #4067 |
| Hidden protocol marker | about 420–2,300 chars of base64 per review (not visible) |
| Gate statuses at exactly 140 chars (truncated) | Every "waiting", "open findings", "not success" and "evaluation error" status sampled |

## 6. Machine contracts on the current text

Anything below must be preserved or migrated with a dual-read period. Theseus has done this before: v0.2.4–v0.2.6 read the old `oracle-*` spellings alongside the new ones through `legacy.py` (see `docs/hosted-workflows.md:203-207` in the local v0.2.4 checkout). Those readers are gone at v0.2.8.

### 6.1 In Theseus

| # | Contract | Reader | Text depended on | If broken |
|---|---|---|---|---|
| T1 | Protocol record | `protocol.py:57-58`, `:343-361`, `:396-412` | `<!-- review-protocol: v1 <base64> -->` at the end of the body, exactly once | History unreadable |
| T2 | Verdict heading | `review_check.py:20-23`, `:89-90`, `:138-146` | Body must **start** with `### review-verdict: <state> for` + backticked 40-hex SHA | A SHIP is no longer "receipt-backed"; the gate stops passing |
| T3 | Thread defect id | `protocol.py:825`, `:854-859`; `cli.py:278-299`, `:1203` | `<!-- review-defect: c<cycle>-D<n> -->` | Disputes lose their key |
| T4 | Thread fingerprint | `dedup.py:11-12`, `:26-31` | `<!-- review-finding: <16 hex> -->` at the end of the thread body | Duplicate threads |
| T5 | Thread title line | `dedup.py:13`, `:30`, `:46`; `cli.py:299` | `^**<Severity>**: <title>$`; first body line (300 chars) is passed to the reviewer as the dispute title (`render.py:341`) | Severity falls back to Low, so findings are re-posted; decoration leaks into the reviewer prompt |
| T6 | Unedited review | `protocol.py:401-402`; `review_check.py:138-146`; `dedup.py:39`; `cli.py:264` | No edit, ever, to a bot review or thread | PR held with "a review was edited" |
| T7 | Dispute and owner grammars | `protocol.py:831-851`; `adjudication.py:9-20`; `cli.py:549-560`; `reset.py:8-15`; `review_check.py:27-28` | `Dispute:`, `Dispute: D<n> - <reason>`, `Review adjudication: …`, `/review`, `Stand-in review: APPROVE <sha>`, `Review backoff reset: <sha>` | Human-typed; the bot must quote them exactly |
| T8 | Status: run suffix | `edits.py:25`, `:67-72`; `review_check.py:24`, `:120-125` | ` [run <id>]` at the very end | Receipt cannot be matched to its status |
| T9 | Status: repost mark | `report.py:802`; `review_check.py:30`, `:195-196`; `cli.py:616` | ` (reposted)` suffix before the run suffix | Stand-in window miscounted |
| T10 | Status: open findings | `review_check.py:29`, `:209-215` | Substring `SHIP WITH FIXES` | A stand-in could waive open findings |
| T11 | Status: escalation | `review_check.py:31`, `:222` | `escalated D1,D2` | Owner-ruling message not shown |
| T12 | Status: exact and prefix strings | `review_check.py:25-26`, `:163`, `:274`, `:292`; `edits.py:24`; `review_check.py:206`; `github.py:494`; `cli.py:2290` | `only exempt paths changed` (exact), `refused after…`, `a review was edited…`, `review-verdict failed twice without a result at: …` (prefixes) | Wrong gate decision or repeated surfacing |
| T13 | Degraded suffix | `report.py:574`, `:865`; `local.py:975-978` | ` · degraded: <providers>` | Local message garbled |
| T14 | Local record | `findings.py:29`, `:42-45`, `:366` | `<!-- review-local-record: v… -->` | Not affected by this proposal |

### 6.2 In BenchBox (so this is not Theseus-only)

| # | Contract | Reader | Text depended on | If broken |
|---|---|---|---|---|
| C1 | Verdict heading | `_project/scripts/soundness_merge_digest.py:22-25`, `:347-348` | Same heading as T2 (three historical spellings) | Review signal not recognised |
| C2 | Verdict heading | `scripts/check_comment_cleanup_scope.py:1592`, `:1625-1627` | Same heading, `review-verdict` only | Cleanup-scope check stops accepting reviewed heads |
| C3 | Decision line | `soundness_merge_digest.py:83`, `:519-522` | `^Decision: **…**` | Falls back to the bare state name |
| C4 | Open-defect sections | `soundness_merge_digest.py:70-82`, `:473-496` | Exact bold section titles, then `^- **<Severity>**` bullets; `<details>` blocks are stripped first | Open severities under-reported |
| C5 | Thread severity | `soundness_merge_digest.py:55`, `:327-329`, `:468-470`, `:915` | Thread body must **start** with `**<Severity>**: ` | **Fails open:** an overridden High finding is no longer classified as an override |
| C6 | Docs | `docs/internal/review-v2.md:746-749`, `:816` | Describes the body format in prose | Stale documentation |

C4 and C5 matter most. The digest reads merged history permanently, so its old-format readers must stay forever; it needs the new format added, not swapped.

BenchBox's digest already imports Theseus's `protocol.decode_marker` (`soundness_merge_digest.py:613`, `:794`). The protocol record already holds the decision and every open defect's severity. The durable fix is for the digest to read the record and stop scraping markdown.

### 6.3 Tests that pin wording (Theseus)

- 20 golden fixtures in `tests/fixtures/review_golden/`, regenerated with `REVIEW_GOLDEN_UPDATE=1` (`tests/integration/golden.py:17-18`, `:168-175`).
- Occurrences across `tests/` (files / hits): verdict heading 29 / 64; "supersedes" line 17 / 39; `Decision: **` 19 / 36; `**Severity**: ` 13 / 51; gate `review: …` messages 25 / 36; `pending: ` 15 / 33; `only exempt paths changed` 7 / 19; "Reviewers not available" 7 / 17; `defect(s) to fix` 8 / 15.
- Heaviest hand-written files: `tests/unit/reviewer/test_report_outcomes.py`, `test_attempts.py`, `test_rounds.py`, `test_dedup.py`, `tests/unit/test_review_check*.py`.
- BenchBox: `tests/unit/scripts/test_soundness_merge_digest.py` and `test_soundness_merge_digest_dispatch.py` pin `still_open_severities` and the heading regex.

## 7. Adversarial critique

**What a reader must get through before the verdict**

- A heading whose content is a status-API state (`failure`) and a 40-character hash. Neither is the decision.
- A sentence about superseding earlier heads, identical on every review.
- GitHub's text preview of a review is therefore `review-verdict: success for 7dc8…` followed by that boilerplate (confirmed from the API's `body_text`).

**Three vocabularies for one outcome**

- `failure` (status state), `SHIP WITH FIXES` (decision) and "defects to fix" all describe the same result.
- `pending` covers at least eight different situations: quota, gateway down, incomplete read, oversize, fork, edited review, failed artifacts, failed tool install. Only some of them resolve by waiting, and the text does not say which.

**Duplication**

- The "supersedes" line: 13 copies on one PR.
- Gateway outage, said four times in 406 characters: once per reviewer tier, then again under "Provider availability".
- The same pending review posted twice on one head.
- Full 40-hex SHAs up to three times per body (heading, scope, carried-from).
- Every gate event posts a fresh status with the same truncated text. BenchBox #4062's head has 29 `review-gate` statuses, and 29 more under the mirrored `oracle-gate` context.

**Jargon the author cannot act on**

- `huge (role review, omp @plan)`, `attempt 3`, `absent (incomplete)`, `slot`, `claude pool blocked`, `google-antigravity`, `recovery: escalate (Jev, probability 0.89, confidence 0.83)`, `degraded:`, `carried`, strike counts stated without the consequence.
- Internal staging paths shown as finding locations: `.review-promotion/new/src/theseus_reviewer/gate.py:204-206`, `.review-pull-request.diff lines 66-74`.
- Failed-attempt logs appear under a successful verdict, at the same visual level as findings.

**Severity**

- A bold word mid-bullet. No mark, no counts, no grouping.
- The status says `7 defect(s) to fix` and never how many are High.
- A reader will assume Low is optional. It is not: the reviewer brief says "every listed defect blocks the merge … the rating orders the list and never decides the outcome" (`templates/gate.md:64-66`). The only hint in the output is one sentence about defects outside the diff.

**How a finding is laid out**

- One paragraph of 560–790 characters mixing mechanism, example, consequence and fix. The fix is always the last sentence.
- Titles run to 136 characters in the sample (median 101; limit 200, `verdict.py:17`), so the "headline" wraps to two lines or more.
- No visible id in the thread, and nothing on the thread says how to contest it. The dispute instructions appear only in the repost note and in a gate status that is cut off before it reaches them.
- "Verified fixed: D1. Resolve their review threads." hands the author a chore and gives no way to find which thread is D1.

**Noise per head**

- Every run creates a new review, and every review creates a notification.
- "Not reviewed" results are full timeline entries, although nothing parses them: they carry no protocol record.
- Old reviews stay fully expanded for ever. On #45 the reader scrolls past 21,500 characters of superseded output to reach the final SHIP.

**Status descriptions**

- Verdict statuses lead with the tier name: `huge: SHIP`.
- Pending statuses contain raw JSON, cut mid-object. On a public PR this also publishes the operator's vendor and subscription state.
- Gate statuses open with a redundant `review:` prefix and a full SHA, so the instruction is truncated: `…or a stand-in attestation 'Stand-in review: APPROVE 7…`.
- One gate message is simply wrong for its case: `the review's latest verdict … is pending, not success; fix the findings` is shown when there are no findings (`review_check.py:281-284`).
- The gate posts state `failure` while it is only waiting (`gate.py:66-67`), so a red cross appears beside the word "waiting".

**Mobile and email**

- 40-hex strings in headings and code spans wrap badly on narrow screens. `[INFERENCE]`
- Only one `<details>` exists in the whole renderer (`report.py:640-649`). Everything else is always expanded.

## 8. Proposal

### 8.1 Principles

1. **Line 1 is the verdict, in the decision vocabulary**, with one fact that tells the author what happens next.
2. **Markers are the API; prose is presentation.** Every machine reader moves to hidden markers or the decoded protocol record. After that the wording can change freely.
3. **One line per finding** in any list: mark, word, id, title, location.
4. **Collapse what the author cannot act on:** reviewer notes, attempt logs, provider detail.
5. **Say each thing once.** No per-review boilerplate, no repeated SHAs, no repeated outage lines.
6. **Verdict reviews stay immutable.** Reduce noise by posting less and by minimising superseded reviews, never by editing them.
7. **Finding ids are visible everywhere** an author might need to type one.

### 8.2 Severity legend

| Level | Mark and label | Blocks merge? | Check annotation level, if mirrored |
|---|---|---|---|
| Critical | 🔴 **Critical** | Yes | `failure` |
| High | 🟠 **High** | Yes | `failure` |
| Medium | 🟡 **Medium** | Yes | `warning` |
| Low | 🔵 **Low** | Yes | `notice` |
| Advisory (pre-existing, if that mode is used) | ⚪ **Advisory** | No | `notice` |

Why this set:

- It is the convention readers already know (Semgrep's `🟠 High severity`, CodeRabbit's `🟠 Major`).
- The word is always present and bold, so meaning survives monochrome, colour-blindness and a missing glyph.
- The severity names are unchanged (`policy.py:12`), so no schema or record changes.
- Because every listed finding blocks regardless of level, **"blocks merge" is stated in the headline**, not implied by the colour. This prevents 🔵 being read as optional.

Alternatives rejected:

- **Shape-coded marks** (🛑 🔶 🟡 🔹): better without colour, but sizes and baselines vary across platforms and the set is unfamiliar.
- **Text only** (`[High]`): right for the terminal, where Theseus already uses it (`docs/local-review.md:39`); weakest for scanning on the web.
- **Image badges:** external dependency, blocked images, no dark variant.
- **Alerts:** see 3.3.

Verdict marks, each a single code point paired with its word: ✅ SHIP, 🔧 SHIP WITH FIXES, ⛔ DO NOT SHIP, ⏳ NOT REVIEWED YET, 🚫 REFUSED.

### 8.3 Mockup (a): the verdict review body

All mockups below were rendered through GitHub's Markdown API. Markers are abbreviated.

**SHIP**

````markdown
### ✅ SHIP · no open findings

Reviewed 7dc848688090de693a109b9cf75bf96d851937a4 · changes since f6c226dd992e7ae2e59420f346150c43114563fb · fixed: **D11**, **D14** (resolve their threads)

<details><summary>Reviewer notes · huge (omp @plan)</summary>

Read both changed files in full, then followed the new protocol.py logic into report.py, runner.py, …

</details>

<!-- review-verdict: v2 success 7dc848688090de693a109b9cf75bf96d851937a4 -->
<!-- review-protocol: v1 eNpNlN2KJDcMhd-… -->
````

The bare SHAs render as 7-character commit links.

**SHIP WITH FIXES** (real data from BenchBox #4062; these six findings have no thread, so their detail is collapsed in the body)

````markdown
### 🔧 SHIP WITH FIXES · 6 findings block merge

**5 High · 1 Medium** · reviewed 855f9776bf80c55bc32f00b12d68f63fc0ed403c · fix each one, or contest it with a comment `Dispute: D<n> - <reason>`

<details><summary>🟠 <b>High</b> · D2 · Detect shared-head collisions independently of the local base policy · <code>src/theseus_reviewer/gate.py:204-206</code></summary>

The collision check runs only when this branch's policy lists several bases, and _rival_bases additionally ignores bases absent from that local policy at line 188. […]

</details>
<details><summary>🟠 <b>High</b> · D3 · Do not manufacture a maintained disposition from a fixed answer · <code>src/theseus_reviewer/protocol.py:1107-1113</code></summary>

demote_forced_fixed rewrites fixed to not_fixed while preserving evidence that […]

</details>
<details><summary>🟠 <b>High</b> · D4 · Unchanged finding lines do not verify pre-existing behavior · <code>src/theseus_reviewer/protocol.py:858-867</code></summary>

[…]

</details>
<details><summary>🟠 <b>High</b> · D5 · Bind accepted live results to trusted execution evidence · <code>tools/review_eval.py:677-684</code></summary>

[…]

</details>
<details><summary>🟠 <b>High</b> · D6 · Include gate, scorer and corpus changes in evaluation enforcement · <code>tools/review_eval.py:640-645</code></summary>

[…]

</details>
<details><summary>🟡 <b>Medium</b> · D7 · Do not report a passing evaluation gate without a baseline · <code>tools/review_eval.py:653-657</code></summary>

[…]

</details>

<details><summary>Reviewer notes · huge (omp @plan)</summary>

The promotion cannot merge unchanged. It retires a status context that BenchBox still requires, […]

</details>

<!-- review-verdict: v2 failure 855f9776bf80c55bc32f00b12d68f63fc0ed403c -->
<!-- review-protocol: v1 eNqtWNuKJLkR_ZU… -->
````

**SHIP WITH FIXES when findings have threads** (the common case; real data from Theseus #45)

````markdown
### 🔧 SHIP WITH FIXES · 1 finding blocks merge

**1 Medium** · reviewed 2aff04c7ce9b8fc267aeeed71661ae03cd069a6a · changes since 8f192e3e1e596e460c67c8103a6685ee6f504735 · fixed: **D1** (resolve its thread)

- 🟡 **Medium** · D2 · Advisory demotion compares exact path strings, so another spelling of a changed file's path skips the… · `src/theseus_reviewer/protocol.py` (thread below)

<details><summary>Reviewer notes · huge (omp @plan)</summary>

One fix is needed before merge. […]

</details>
<details><summary>Run details · 3 earlier attempts did not finish</summary>

- large, attempt 1: did not read 86 of 146 changed hunks
- large, attempt 2: did not read 1 of 146 changed hunks
- huge, attempt 3: did not read 86 of 146 changed hunks

</details>

<!-- review-verdict: v2 failure 2aff04c7ce9b8fc267aeeed71661ae03cd069a6a -->
<!-- review-protocol: v1 … -->
````

Rules for the finding list:

- New findings with threads: one line each, no detail (the thread is directly below).
- Findings still open from earlier reviews: the same one-line form under a `**Still open**` label.
- Findings with no thread: the collapsed form above.
- Titles are display-truncated at about 90 characters in the list only. The thread shows the full title, and the fingerprint still uses the full title.
- Staging prefixes such as `.review-promotion/new/` are removed from displayed paths.

**DO NOT SHIP** (illustrative text; no real DO NOT SHIP review was in the sample)

````markdown
### ⛔ DO NOT SHIP · rework needed · strike 2 of 3

The change replaces the gate's fail-closed default with a fallback that passes when the policy cannot be read. Patching individual call sites will not fix that; the fallback has to go.

**Next:** rework and push. A new head gets a full review. A third DO NOT SHIP closes this PR to further review.

<!-- review-verdict: v2 failure … -->
<!-- review-protocol: v1 … -->
````

Here the reviewer's reason is the content, so it stays visible (it is capped at 1,500 characters by the brief).

**Not reviewed**

````markdown
### ⏳ NOT REVIEWED YET · reviewers unavailable

No action needed: the hourly sweep retries this head. A maintainer can comment `/review` to retry now.

<details><summary>What happened · 1 attempt failed, 2 skipped</summary>

| Reviewer | Result |
|---|---|
| large | provider quota exhausted (HTTP 429) |
| huge, max | skipped: same provider pool is blocked |

</details>

<!-- review-verdict: v2 pending 40a8b14def5595cc5d839608c46da7916ae01bd7 -->
````

Rules:

- Raw provider payloads never reach GitHub. Each absence maps to a short human reason (quota, gateway unreachable, did not finish reading, invalid answer, timeout). The raw text stays in the run log.
- Skip the post when the previous bot post for the same head has the same cause.
- The second sentence changes for causes that waiting will not fix: "Split the PR, or ask an owner for a stand-in" (oversize); "Push a new head" (edited review).
- `[INFERENCE]` The next retry time could be shown; the retry state appears to hold it (`retry.State`, pool block times). Not verified.

**Carried**

````markdown
### ✅ SHIP · carried from 8f192e3e1e596e460c67c8103a6685ee6f504735

Same patch as that head (only the base moved), so no reviewer ran.

<!-- review-verdict: v2 success … -->
<!-- review-protocol: v1 … -->
````

With open findings the headline becomes `### 🔧 SHIP WITH FIXES · 2 findings still block merge · carried from …`, followed by the one-line list.

**Remaining states: headline only**

| State | Headline | One line beneath |
|---|---|---|
| Owner ruling | `### ✅ SHIP · owner ruling: WITHDRAW D2` | The owner's reason; any findings still open |
| Refused | `### 🚫 REFUSED · 3 DO NOT SHIP decisions` | "Close this PR and open a new one with the reworked change." |
| Withheld | `### ⏳ NOT REVIEWED YET · run artifacts failed validation` | "A maintainer can comment `/review` to rerun." Errors collapsed |
| Oversize | `### ⏳ NOT REVIEWED · too large to review` | "Split the PR, or ask an owner for a stand-in. This head is not retried." |
| Repost | `### 🔧 SHIP WITH FIXES · unchanged (no reviewer ran)` | "To get a new judgement, push a change, or dispute a finding and comment `/review`." |
| Shadow mode | Any headline + ` · shadow (advisory)` | Nothing else |
| Step-back round | Normal headline | One visible line: "Round 4 with findings: reconsider the approach rather than patching one finding at a time." |

### 8.4 Mockup (b): an inline finding thread

**Step 1: renderer only, no reviewer changes.** Same text as today, plus the mark, the id and a one-line footer.

````markdown
🟠 **High** · D1 · Migrate BenchBox's required status before retiring oracle-gate

This pin switches the producer to a release that no longer posts oracle-gate. […unchanged detail…]

<sub>Blocks merge · to contest, reply starting <code>Dispute:</code> with file:line evidence</sub>

<!-- review-defect: c1-D1 -->

<!-- review-finding: f9318c216fdba137 -->
````

This does not shorten the thread. It fixes the invisible id and the missing "how to contest".

**Step 2: structured detail.** Requires the reviewer brief to ask for problem, impact and fix separately (see 8.7). The text below was rewritten by hand from the real thread to show the target.

````markdown
🟠 **High** · D1 · Migrate BenchBox's required status before retiring `oracle-gate`

**Problem:** this pin moves to a release that no longer posts `oracle-gate`, but BenchBox still requires that context.

**Impact:** new heads can never satisfy the required checks, and old heads keep a retired status that is never refreshed. The caller's posting check accepts either context, so the gap stays hidden.

**Fix:** finish the ruleset, enforcement-script, caller and docs cutover before installing this pin.

<details><summary>Evidence · 3 locations</summary>

- `docs/internal/repo-admin-settings.md:60-65`, `:174-175` still require both contexts
- `_project/scripts/ruleset_review_enforcement.py:11`, `:58-59` insist on `oracle-gate`
- `scripts/ruleset_drift_check.py` checks that binding

</details>

<sub>Blocks merge · to contest, reply starting <code>Dispute:</code> with file:line evidence</sub>

<!-- review-defect: c1-D1 -->

<!-- review-finding: f9318c216fdba137 -->
````

Before and after for this thread: 827 visible chars with one 753-character paragraph, to 629 visible chars with no paragraph over 197.

The core statement is deliberately **not** collapsed. In a thread the reader is at the code and wants the reasoning; every sampled tool keeps it visible and collapses only supporting material.

**Step 3: suggestion block, when the fix is mechanical.** The finding below is invented for illustration.

`````markdown
🟡 **Medium** · D4 · Retry loop stops one attempt early

**Problem:** `range(attempts)` yields `attempts - 1` retries after the first try.

**Fix:**

````suggestion
    for attempt in range(1, attempts + 1):
````

<sub>Blocks merge · to contest, reply starting <code>Dispute:</code> with file:line evidence</sub>
`````

Conditions, all required:

- The comment's line range is exactly the lines replaced, all on the right side of the diff (the span logic already exists: `verdict.py:586`, `report.py:337-340`).
- The fence is longer than any backtick run in the replacement, so the text cannot break out.
- Off by default, enabled per consumer policy, and never on gate or soundness paths.
- The reviewer supplies the replacement in a new optional field; the renderer never derives code from prose.

The risk is trust, not syntax: a one-click commit of model-written code, co-authored by the review App, on a system whose reviewer input is partly attacker-controlled.

### 8.5 Mockup (c): status descriptions

All proposed strings are at most 140 characters and were checked against the parser contracts T8–T12 (run suffix, repost mark, `SHIP WITH FIXES` substring, `escalated D…` pattern, exact and prefix strings).

**`review-verdict`**

| Case | Today | Chars | Proposed | Chars |
|---|---|---|---|---|
| SHIP | `huge: SHIP [run 42512404919]` | 28 | `SHIP · no open findings [run 42512404919]` | 41 |
| SHIP after fixes | same | 28 | `SHIP · D11, D14 fixed · no open findings [run 42512404919]` | 58 |
| SHIP WITH FIXES | `huge: SHIP WITH FIXES, 7 defect(s) to fix` | 41 | `SHIP WITH FIXES · 7 to fix (5 High, 1 Medium, 1 Low) · see review threads` | 73 |
| Escalated | `huge: SHIP WITH FIXES, 2 defect(s) to fix; escalated D1,D2` | 58 | `SHIP WITH FIXES · 2 to fix · escalated D1,D2 · owner ruling needed` | 66 |
| DO NOT SHIP | `huge: DO NOT SHIP` | 17 | `DO NOT SHIP · rework needed · strike 2 of 3` | 43 |
| Refused | `refused after 3 DO NOT SHIP decisions: open a new pull request` | 62 | `refused after 3 DO NOT SHIP decisions · open a new pull request` | 63 |
| Quota | `pending: large: absent (quota: 429 Cloud Code Assist API error (429): { "error": { "code": 429, "message": "Individual quota reached. Pleas…` | 140 | `Not reviewed yet · reviewers unavailable (provider quota) · retries hourly · /review retries now` | 96 |
| Gateway down | `pending: no reviewer available (gateway unreachable)` | 52 | `Not reviewed yet · model gateway unreachable · retries hourly · /review retries now` | 83 |
| Incomplete read | `pending: medium: absent (incomplete: the verdict would ship but 2 of 2 changed hunks in 2 required files were not seen: benchbox/core/equiv…` | 140 | `Not reviewed yet · reviewer did not read every changed hunk (3 attempts) · retries hourly` | 89 |
| Oversize | `pending: too large for the review; split the pull request or ask the owner for a stand-in` | 89 | `Not reviewed · too large to review · split the PR or ask an owner for a stand-in` | 80 |
| Carried SHIP | `carried from 8f192e3e1e59: SHIP [run …]` | 49 | `SHIP · carried from 8f192e3e1e59 (same patch) [run 42512404919]` | 63 |
| Carried with findings | `carried from 8f192e3e1e59: SHIP WITH FIXES` | 42 | `SHIP WITH FIXES · 2 to fix · carried from 8f192e3e1e59 (same patch)` | 67 |
| Owner ruling | `owner adjudication: WITHDRAW D2; SHIP [run …]` | 55 | `SHIP · owner ruling: WITHDRAW D2 [run 42512404919]` | 50 |
| Repost | `huge: SHIP (reposted) [run …]` | 39 | `SHIP · no open findings (reposted) [run 42512404919]` | 52 |
| Exempt | `only exempt paths changed` | 25 | unchanged (exact match is parsed) | 25 |
| Fork | `fork: owner review` | 18 | `Not reviewed · fork PR · needs an owner stand-in` | 48 |
| Edited review | `a review was edited` | 19 | `a review was edited · verdict withheld · push a new head` | 56 |
| Withheld | `result withheld: artifacts failed validation` | 44 | `Not reviewed · run artifacts failed validation · /review to rerun` | 65 |
| Sweep failure | `review-verdict failed twice without a result at: …` | 95 | unchanged (prefix is parsed) | 95 |

**`review-gate`** (no parser reads these in Theseus source; 36 test assertions pin them)

| Case | Today (all cut at 140) | Proposed | Chars |
|---|---|---|---|
| Pass | `review: pass (review verdict for <40 hex>)` | `Pass · review-verdict: SHIP` | 27 |
| Pass, stand-in | `review: pass (stand-in review attested by joeharris76 for <40 hex>)` | `Pass · stand-in review by joeharris76` | 37 |
| Pass, exempt | `review: only exempt paths changed` | `Pass · only exempt paths changed` | 32 |
| Waiting | `review: waiting for the review's verdict on <40 hex>, or a stand-in attestation 'Stand-in review: APPROVE 7…` | `Waiting for review-verdict · owner bypass: 'Stand-in review: APPROVE <40 hex>'` | 110 |
| Not reviewed | `review: the review's latest verdict for <40 hex> is pending, not success; fix the findings, or post a stand…` | `Waiting · not reviewed yet (reviewers unavailable) · retries hourly · owner may post a stand-in` | 95 |
| Open findings | `review: the review's latest verdict for <40 hex> lists open findings, which a stand-in cannot waive: fix th…` | `Blocked · open review findings · fix them or reply 'Dispute:' in the thread (a stand-in cannot waive findings)` | 110 |
| Escalated | `review: D1,D2 on <40 hex> stayed open through repeated dispute re-reviews and need the owner's ruling: post 'Review adjud…` | `Blocked · D1,D2 need an owner ruling: 'Review adjudication: WITHDRAW\|MAINTAIN D<n> - <reason>', or push a fix` | 109 |
| DO NOT SHIP | `review: the review's latest verdict for <40 hex> is failure, not success; fix the findings, or post a stand…` | `Blocked · DO NOT SHIP · rework and push a new head, or owner stand-in` | 69 |
| Refused | `review: the review refused to review <40 hex> after repeated DO NOT SHIP decisions; close this pull request and…` | `Blocked · review refused after repeated DO NOT SHIP · open a new PR, or owner stand-in` | 86 |
| Gate error | `review-gate: evaluation error: cannot load the base review policy: GitHub returned HTTP 404 for https://api.github.com/repos/BenchBox-dev/b…` | `Gate error · cannot load the base review policy (HTTP 404) · see the run log` | 76 |

What changed:

- The decision or state comes first. The tier name is gone.
- The SHA is dropped where the status is already attached to that commit, and kept once where the author must copy it (the stand-in grammar now fits in full).
- Counts by severity appear in the status.
- No raw provider text.
- No emoji: the status icon already shows pass, fail or pending, and plain text keeps the parsers simple.

A separate observation, outside formatting: the gate reports `failure` while waiting. Changing it to `pending` would remove the contradictory red cross, but it may affect auto-merge and the digest, so it needs its own decision.

### 8.6 One sticky comment, or a comment per head?

**Editing the verdict review in place is not possible.** The review is a receipt: it carries the protocol record and must never be edited (T6; also `docs/consumer-policy.md:59`). The redesign therefore uses three measures, in this order.

1. **Post less.** Verdict reviews shrink as in 8.3. "Not reviewed" results are posted at most once per head and cause.
2. **Minimise superseded reviews.** After a new verdict posts, mark the bot's earlier reviews on that PR as outdated with the GraphQL `minimizeComment` mutation. Each collapses to a single line that a reader can expand.
   - `PullRequestReview` is minimisable (schema introspection).
   - Evidence that minimising is not an edit: of 213 minimised issue comments sampled on `cli/cli`, 212 had `lastEditedAt: null` and `includesCreatedEdit: false`.
   - `[INFERENCE]` The same holds for reviews, and their inline threads stay visible. **Both must be tested in a scratch repo first**, because a wrong guess holds every PR with "a review was edited".
3. **Optionally add one sticky dashboard comment** per PR, edited in place and explicitly non-authoritative. In the mockup the finding titles and thread links are real (Theseus #54); the counts and history rows are illustrative.

````markdown
<!-- review-summary: v1 -->
### 🔧 Review status: SHIP WITH FIXES · 2 open · head a85889736d54d77ebb0eea4510f38f5a2c92ecbc

- 🟠 **High** · [D3](https://github.com/joeharris76/theseus/pull/54#discussion_r4518790849) · A failed baseline authentication turns the active score rule into a pass · `tools/review_eval.py:714`
- 🟡 **Medium** · [D2](https://github.com/joeharris76/theseus/pull/54#discussion_r4517662261) · Configuration changes pass the score rule unconditionally · `tools/review_eval.py:684`

To clear: push a fix, or reply starting `Dispute:` in the thread.

<details><summary>History · 4 rounds · 1 fixed</summary>

| Round | Head | Verdict |
|---|---|---|
| 4 | a858897 | SHIP WITH FIXES · 2 open |
| 3 | f4d5802 | SHIP WITH FIXES · 2 open |
| 2 | 1d0820e | SHIP WITH FIXES · 2 open |
| 1 | 857cc9c | SHIP |

</details>
````

Its advantages: it can link each finding to its thread (thread URLs exist only after the review is posted, and the review cannot be edited to add them); edits send no notification; and it is the natural home for "not reviewed" status, which would then need no review at all.

Its costs:

- BenchBox's gate workflow triggers on `issue_comment` with `types: [created, edited, deleted]` (`.github/workflows/review-gate.yml:10-11`). Every dashboard edit starts a workflow run, which the job-level `if:` then skips.
- The body must never begin with a command prefix (`/review`, `Review adjudication:`, `Stand-in review: APPROVE`), because workflow conditions match on prefixes (`docs/hosted-workflows.md:236`).
- Two sources of truth. It must state that the review and the status are authoritative.
- BenchBox's design doc says a run with no verdict still posts a review (`docs/internal/review-v2.md:364-368`). Moving that to the dashboard changes a documented contract.

Recommendation: do 1 and 2 first. They remove most of the noise at low risk. Add the dashboard only if the timeline still feels heavy afterwards.

### 8.7 Finding structure (reviewer contract)

Rendering alone cannot turn a 750-character paragraph into problem, impact and fix. Two options:

- **Brief only (smaller):** ask the reviewer to write `detail` as three short paragraphs beginning `Problem:`, `Impact:`, `Fix:`, and titles of at most 80 characters. No schema change. The renderer bolds the labels when present and falls back to today's layout when not.
- **Schema (cleaner):** add optional `impact`, `fix` and `evidence` keys. There is precedent for optional finding keys (`verdict.py:38-39`). This touches validation, three templates, the eval corpus and the goldens.

Start with the brief-only option. For gate reviews, also ask for a one-sentence lead before the bypass analysis, so the renderer can show the lead and collapse the rest. The bypass analysis itself stays mandatory (`templates/gate.md:59-60`); only its position on the page changes.

Reviewer-brief changes alter model behaviour, so they belong behind the review-eval gate, not in a formatting PR.

### 8.8 Check runs

A check run offers a 65,535-character markdown summary and line annotations with three levels, without notifications or timeline entries. It is not a replacement here:

- Annotations cannot be replied to, and disputes need a thread.
- The gate's trust model is built on commit statuses restricted to the App (`docs/hosted-workflows.md:116`).
- It needs a new `checks: write` App permission, which every consumer must approve.

It could mirror findings that have no thread as annotations. That is the lowest-value item on the list.

## 9. Recommendations, ranked by value for effort

| Rank | Change | Value | Effort | Scope |
|---|---|---|---|---|
| 1 | **Dual-read parsers.** Theseus: accept a `review-verdict: v2` marker as well as the heading (T2); accept the new thread title line (T5). BenchBox: C1–C5 read both forms; better, read the protocol record for C3 and C4. | Unblocks everything; closes the C5 fail-open before it can occur | Small | Both repos, BenchBox first |
| 2 | **Status descriptions** (8.5) | High: the checks box is the most-seen surface; fixes truncation and the JSON leak | Small: strings and tests | Theseus only |
| 3 | **Verdict-first body:** new headline, drop the "supersedes" line, collapse reviewer notes and run details, severity marks and counts, one-line finding lists, strip staging prefixes | Highest reader impact (-82% to -92% visible text) | Medium: `report.py` render path, goldens | Theseus, after 1 |
| 4 | **Visible ids and dispute footer in threads** (8.4 step 1) | High: fixes a real usability bug | Small | Theseus, after 1 |
| 5 | **"Not reviewed" output:** human reasons, no provider payloads, skip identical repeats | High on bad days; removes a public leak | Small to medium: map absence kinds to reasons | Theseus only |
| 6 | **Minimise superseded reviews** | High on long PRs (13 reviews become 1 open and 12 one-line stubs) | Small code, but needs the scratch-repo test in 8.6 | Theseus only |
| 7 | **Structured findings via the brief** (8.7) | Medium to high: this is what actually shortens threads | Medium; needs an eval run | Theseus only |
| 8 | **Sticky dashboard comment** | Medium | Medium to high: new post/edit path, workflow interactions | Theseus, plus consumer workflow review |
| 9 | **Suggestion blocks** | Low to medium: few review findings are mechanical | High, with a trust risk | Theseus, opt-in |
| 10 | **Check-run mirror** | Low | High; new App permission | All consumers |

Items 2 and 5 can ship before item 1. They do not touch any parsed body text.

## 10. Risks and migration

### 10.1 Order of operations

1. **BenchBox PR:** make `soundness_merge_digest.py` and `check_comment_cleanup_scope.py` read both formats. Old-format readers stay permanently in the digest. Update `docs/internal/review-v2.md`.
2. **Theseus release A:** dual-read in `review_check.py` and `dedup.py`; new status descriptions; "not reviewed" clean-up. Body format unchanged.
3. **BenchBox lock promotion** to release A.
4. **Theseus release B:** new body and thread format; goldens regenerated.
5. **BenchBox lock promotion** to release B. It is safe only because step 1 has merged.

BenchBox controls when it takes each release through the lock, so the order is enforceable. Lock promotions also go through Theseus's own promotion review (`review_check.py:252-254`).

### 10.2 Risks

| Risk | Consequence | Mitigation |
|---|---|---|
| New thread title line reaches BenchBox before C5 is updated | Overrides of High findings silently stop being classified | Step 1 first; add a digest test that fails on an unrecognised bot thread |
| New body reaches the gate before T2 dual-read | A SHIP is not receipt-backed; PRs wait | Release A before release B; keep one release of overlap |
| Hidden verdict marker spoofed through model text | A forged state or SHA | Already covered: model text has `<!--` and `-->` neutralised (`verdict.py:188-189`). Require exactly one marker, as `decode_marker` does (`protocol.py:343-348`) |
| Model text closes a `<details>` early | Broken layout only | Neutralise `<details` and `</details>` in model text placed inside a collapsible |
| Minimising counts as an edit | Every PR held | Test in a scratch repo; ship behind a policy flag |
| Old and new bodies on one PR mid-migration | Mixed timeline | Acceptable; parsers read both |
| Thread title line change alters the dispute prompt | Decoration reaches the reviewer (`cli.py:299`, `render.py:341`) | Take the title from the parsed line, not the raw first line |
| Displaying a truncated title | Fingerprint mismatch | Truncate for display only; the fingerprint keeps the full title (`dedup.py:16-23`) |
| Status wording drift | Gate misreads a verdict | Proposed strings were checked against T8–T12; keep those tokens as named constants shared by writer and reader |
| Reviewer-brief change shifts verdict quality | Different findings | Ship item 7 separately, through the eval gate |

### 10.3 What to update

**Theseus**

- `src/theseus_reviewer/report.py` (most of the change), `dedup.py:13`, `review_check.py:20-23` and `:249-310`, `cli.py:299` and `:1902-1906`, `sweep.py:109-114`, `gate.py:301`.
- 20 golden fixtures (`REVIEW_GOLDEN_UPDATE=1`), plus hand-written assertions in 24 test modules.
- Docs: `docs/hosted-workflows.md`, `docs/consumer-policy.md` and `docs/local-review.md` each mention the heading, the markers or the grammars.
- A new consumer-facing page that defines the marker grammar as the supported interface and says the prose is not.

**BenchBox**

- `_project/scripts/soundness_merge_digest.py:22-25`, `:55`, `:70-86`, `:473-522`.
- `scripts/check_comment_cleanup_scope.py:1592`.
- `tests/unit/scripts/test_soundness_merge_digest*.py`.
- `docs/internal/review-v2.md:746-749`, `:816`.

### 10.4 Size estimate

`[INFERENCE]` Rough, from the audit above rather than a prototype.

| Piece | Source lines changed | Test lines changed | Notes |
|---|---|---|---|
| Item 1, Theseus | 40–80 | 60–120 | Two regexes and a marker |
| Item 1, BenchBox | 60–120 | 80–150 | More if the digest moves to the protocol record |
| Item 2 | 60–100 | 100–200 | Mostly assertion strings |
| Items 3 and 4 | 250–350 | 200–400 plus 20 regenerated goldens | The bulk of the work |
| Item 5 | 80–150 | 80–150 | Absence-kind mapping |
| Item 6 | 60–100 | 60–100 | One GraphQL call and a policy flag |

Items 1 to 6 together are on the order of two to three focused pull requests in Theseus and one in BenchBox.

## 11. Open questions and things not verified

- **Email and GitHub Mobile rendering** of `<details>`, emoji and alerts was not tested.
- **Minimising a review:** that it leaves `lastEditedAt` null and leaves threads visible is inferred from issue-comment data.
- **Task-list ticks set `lastEditedAt`:** inferred from how task lists work; not tested.
- **The 140 and 65,536 limits** come from integrator reports and Theseus's own constants, not from a GitHub docs page.
- **SHIP record and state equivalence:** the proposal assumes a review whose protocol record says `SHIP` for the head always had state `success`. A test should confirm it before the heading is retired.
- **Does anything need "not reviewed" reviews to exist?** Theseus's history reader ignores them (no protocol record). BenchBox's digest recognises their section titles (`soundness_merge_digest.py:80`) but was not traced far enough to rule out a dependency.
- **Other consumers:** only BenchBox was checked for downstream parsers.
- **Copilot's inline severity:** the sampled inline body had no severity text, so the label is presumably drawn by GitHub's UI.
- **SonarQube Cloud** was observed posting one comment per PR; whether it edits that comment in place was not observed.
