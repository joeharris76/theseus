# Review markers

Theseus posts reviews and review threads whose visible text is presentation and whose HTML-comment markers are the machine interface. A consumer that needs to know a verdict, a finding's identity or a protocol record reads a marker. It does not parse prose. Every review body written after 0.2.8 carries the `review-verdict: v2` marker and no longer starts with the `### review-verdict: <state> for `<40hex>`` heading; that heading is only what 0.2.8 and earlier wrote, and readers must keep accepting it for older reviews. For every review with a protocol record, the verdict is also in its `review-protocol` record (`decision`, `head_sha`). The prose (headings, wording, ordering, emoji, tables, collapsed sections) may change in any release without notice; the grammars below change only by a new version.

Every marker is an HTML comment, so GitHub does not render it. Model text cannot forge one: the reviewer sanitizes every model-supplied string and rewrites `<!--` and `-->` to `&lt;!--` and `--&gt;` before it reaches a body, so a raw marker in a posted body was written by Theseus. Trust a marker only on a body authored by the review App, unedited (`last_edited_at` null and `includes_created_edit` false). Theseus itself never trusts an edited bot review.

Consumers MUST ignore markers whose name or version they do not know, and MUST NOT depend on the position of a marker in the body unless the table says so.

| Marker | Lives on | Meaning |
|---|---|---|
| `review-protocol: v1` | review body | the decision record |
| `review-verdict: v2` | review body | the verdict state for one head (written on every verdict body; read by the gate) |
| `review-unavailable: v1` | review body | written on every not-reviewed post: the head was not reviewed because no reviewer was available |
| `review-defect` | review thread comment | the defect id the thread belongs to |
| `review-finding` | review thread comment | the finding fingerprint used for deduplication |
| `review-local-record: v1` | pull request body | the local review's findings record |

## `review-protocol: v1`

```
<!-- review-protocol: v1 <data> -->
```

`<data>` is URL-safe base64 (padding removed) of zlib-compressed JSON. It is the last thing in the body, followed only by whitespace. Exactly one per review; a body with zero is not a protocol review and a body with more than one, or one that fails to decode or validate, is invalid and treated as unreadable. The record carries `v`, `cycle`, `round`, `kind`, `decision` (`SHIP`, `SHIP_WITH_FIXES`, `DO_NOT_SHIP`, `REFUSED`), `head_sha`, `base_ref`, `reviewer`, `tier`, `strikes_after`, `open_defects`, `next_id`, `patch_digest`, `carried`, `summary` and, when the run is known, `run_uuid`. Read the decision and the open defects from here, never from the body text. The format is unchanged.

## `review-verdict: v2`

```
<!-- review-verdict: v2 <state> <40hex> -->
```

`<state>` is `success`, `failure` or `pending`, matching the commit status the App posts to the `review-verdict` context; `<40hex>` is the full lowercase head SHA the verdict is for.

Reading rules, which the gate applies when it decides whether a review is a receipt for a head:

- A body carries at most one `review-verdict` marker, anywhere in the body. Two or more, or one that is malformed, is not a receipt.
- The older heading form `### review-verdict: <state> for `<40hex>`` at the very start of the body is equally accepted. Reviews written after 0.2.8 do not carry it.
- A body that carries both forms is a receipt only if they agree on state and SHA. Any disagreement is not a receipt.
- The SHA must equal the head under test, and the state must be `success` for a passing receipt.
- A receipt is additionally bound to the verdict status by the `run_uuid` in its `review-protocol` record, and must be unedited.

For every review with a protocol record, `decision` is `SHIP` if and only if the verdict state is `success`.

The marker sits on its own line immediately before the `review-protocol` record, so the record stays the last thing in the body. A "not reviewed" body (no reviewer ran, no protocol record) carries the verdict marker (state `pending`) and then the `review-unavailable` marker, which is last.

### What the body looks like (presentation, not interface)

The first line is `### <mark> <DECISION> · <one fact>` with the marks ✅ SHIP, 🔧 SHIP WITH FIXES, ⛔ DO NOT SHIP, ⏳ NOT REVIEWED (YET) and 🚫 REFUSED. A second line gives severity counts, the reviewed head and, when findings are open, how to contest them (`Dispute: D<n> - <reason>`). Each finding is one line, most severe first: `<mark> **<Severity>** · D<n> · <title> · <path>`, with 🔴 Critical, 🟠 High, 🟡 Medium, 🔵 Low and ⚪ Advisory. Every listed finding blocks the merge whatever its severity, and the severity word is always printed next to the mark. A finding without a review thread keeps its detail in a collapsed block. The reviewer's notes and the attempt log are collapsed. Model-written text placed in a body cannot open or close a collapsible, start a heading or a comment, or form a task-list checkbox: ticking one would edit the review, and Theseus treats an edited review as tampering.

## `review-unavailable: v1`

```
<!-- review-unavailable: v1 <40hex> <cause-key> -->
```

Written on every not-reviewed post: the head `<40hex>` was not reviewed because no reviewer could run, for the cause `<cause-key>`. The cause key is the sorted set of `[a-z0-9-]` cause keys joined by `+` (for example `quota+timeout`). It is never a receipt and never carries a verdict.

The post is not repeated: when the latest post by the review App for the same head already carries the same head and cause key, `drive publish` does not post again. Only markers are compared, never prose; see [hosted-workflows.md](hosted-workflows.md).

## `review-defect`

```
<!-- review-defect: c<cycle>-D<n> -->
```

On a review thread comment: the defect `D<n>` (n from 1 to 9999) opened in review cycle `<cycle>`. Disputes and replies address `D<n>`. Unchanged.

## `review-finding`

```
<!-- review-finding: <16hex> -->
```

Last thing in a thread comment. `<16hex>` is the first 16 hex digits of `sha256("<path>\0<normalised title>")`, where the title is lowercased and every run of characters outside `a-z0-9` becomes one space, then trimmed. It is how Theseus recognises a finding it has already posted. Unchanged; changing the title wording never changes it for an existing thread, because Theseus reads the fingerprint from the marker.

When a thread has no `review-finding` marker (threads from older releases), Theseus recovers the fingerprint from the thread's title line, which it accepts in either form:

```
**<Severity>**: <title>
<mark> **<Severity>** · D<n> · <title>
```

`<Severity>` is `Critical`, `High`, `Medium` or `Low`; `<mark>` is one of 🔴 🟠 🟡 🔵 ⚪; `D<n>` is a defect id. Both forms yield the same severity and title, and the title alone (without decoration) is what Theseus hashes and shows to the reviewer in a dispute. A title in neither form gives no fingerprint (the thread is not recognised as a posted finding), and the dispute brief shows the thread's first line as written. When both forms appear in one body, the earliest one wins.

Threads written after 0.2.8 use the second form. The detail follows, then a footer line, `<sub>Blocks merge · to contest, reply starting <code>Dispute:</code> with file:line evidence</sub>`, then the `review-defect` and `review-finding` markers. The footer is not a dispute: a dispute is a reply whose line starts with `Dispute:`, and the footer line starts with `<sub>`.

## `review-local-record: v1`

```
<!-- review-local-record: v1 <data> -->
```

The local review's findings record, carried in a pull request body: URL-safe base64 of a zlib-compressed JSON object with `v`, `patch_digest` and `findings` (id, status, title, path and dispositions per finding). Exactly one per body (a body with none, or more than one, carries no record); it is capped at 30000 characters. The record is the interface; the surrounding prose is presentation.
