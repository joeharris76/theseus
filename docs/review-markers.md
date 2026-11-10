# Review markers

Theseus posts reviews and review threads whose visible text is presentation and whose HTML-comment markers are the machine interface. A consumer that needs to know a verdict, a finding's identity or a protocol record reads a marker. It does not parse prose. The prose (headings, wording, ordering, emoji, tables, collapsed sections) may change in any release without notice; the grammars below change only by a new version.

Every marker is an HTML comment, so GitHub does not render it. Model text cannot forge one: the reviewer sanitizes every model-supplied string and rewrites `<!--` and `-->` to `&lt;!--` and `--&gt;` before it reaches a body, so a raw marker in a posted body was written by Theseus. Trust a marker only on a body authored by the review App, unedited (`last_edited_at` null and `includes_created_edit` false). Theseus itself never trusts an edited bot review.

Consumers MUST ignore markers whose name or version they do not know, and MUST NOT depend on the position of a marker in the body unless the table says so.

| Marker | Lives on | Meaning |
|---|---|---|
| `review-protocol: v1` | review body | the decision record |
| `review-verdict: v2` | review body | the verdict state for one head |
| `review-unavailable: v1` | review body | reserved: the head was not reviewed because no reviewer was available |
| `review-defect` | review thread comment | the defect id the thread belongs to |
| `review-finding` | review thread comment | the finding fingerprint used for deduplication |
| `review-local-record: v1` | pull request body | the local review's findings record |

## `review-protocol: v1`

```
<!-- review-protocol: v1 <data> -->
```

`<data>` is URL-safe base64 (padding removed) of zlib-compressed JSON. It is the last thing in the body, followed only by whitespace. Exactly one per review; a body with zero is not a protocol review and a body with more than one, or one that fails to decode or validate, is invalid and treated as unreadable. The record carries `v`, `cycle`, `round`, `kind`, `decision` (`SHIP`, `SHIP_WITH_FIXES`, `DO_NOT_SHIP`, `NONE`), `head_sha`, `base_ref`, `reviewer`, `tier`, `strikes_after`, `open_defects`, `next_id`, `patch_digest`, `carried`, `summary` and, when the run is known, `run_uuid`. Read the decision and the open defects from here, never from the body text. The format is unchanged.

## `review-verdict: v2`

```
<!-- review-verdict: v2 <state> <40hex> -->
```

`<state>` is `success`, `failure` or `pending`, matching the commit status the App posts to the `review-verdict` context; `<40hex>` is the full lowercase head SHA the verdict is for.

Reading rules, which the gate applies when it decides whether a review is a receipt for a head:

- A body carries at most one `review-verdict` marker, anywhere in the body. Two or more, or one that is malformed, is not a receipt.
- The older heading form `### review-verdict: <state> for `<40hex>`` at the very start of the body is equally accepted.
- A body that carries both forms is a receipt only if they agree on state and SHA. Any disagreement is not a receipt.
- The SHA must equal the head under test, and the state must be `success` for a passing receipt.
- A receipt is additionally bound to the verdict status by the `run_uuid` in its `review-protocol` record, and must be unedited.

For every review with a protocol record, `decision` is `SHIP` if and only if the verdict state is `success`.

## `review-unavailable: v1`

```
<!-- review-unavailable: v1 <40hex> <cause-key> -->
```

Reserved for the review-format release that adds it: the head `<40hex>` was not reviewed because no reviewer could run, for the cause `<cause-key>` (a lowercase token naming why). It is never a receipt and never carries a verdict. Releases that do not emit it simply never post it; consumers MUST ignore it until they implement it.

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

`<Severity>` is `Critical`, `High`, `Medium` or `Low`; `<mark>` is one of 🔴 🟠 🟡 🔵 ⚪; `D<n>` is a defect id. Both forms yield the same severity and title, and the title alone (without decoration) is what Theseus hashes and shows to the reviewer in a dispute. A first line in neither form is used as written.

## `review-local-record: v1`

```
<!-- review-local-record: v1 <data> -->
```

The local review's findings record, carried in a pull request body: URL-safe base64 of a zlib-compressed JSON object with `v`, `patch_digest` and `findings` (id, status, title, path and dispositions per finding). Exactly one per body (a body with none, or more than one, carries no record); it is capped at 30000 characters. The record is the interface; the surrounding prose is presentation.
