### 🔧 SHIP WITH FIXES · 1 finding blocks merge

**1 Medium** · reviewed aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa · changes since 8f192e3e1e596e460c67c8103a6685ee6f504735 · fixed: **D1** (resolve its thread) · fix each one, or contest it with a comment `Dispute: D<n> - <reason>`

- 🟡 **Medium** · D2 · Advisory demotion compares exact path strings, so another spelling of a changed file's pa… · `benchbox/core/equivalence/checker.py` (thread below)

<details><summary>Reviewer notes · small (omp @smol)</summary>

Reviewer: small (role review, omp @smol).

One fix is needed before merge. Read both changed files in full, then followed the new protocol logic into the report writer, the runner and the gate. Checked each bypass scenario against the code as changed: a step-back round widens the scope to the whole change and keeps every prior id; a forced round demotes a first-round fix to not fixed when the cited evidence is missing; a withdrawn finding must cite a file and line; a path spelled differently from a changed file's path is compared after normalisation. Read both changed files in full, then followed the new protocol logic into the report writer, the runner and the gate. Checked each bypass scenario against the code as changed: a step-back round widens the scope to the whole change and keeps every prior id; a forced round demotes a first-round fix to not fixed when the cited evidence is missing; a withdrawn finding must cite a file and line; a path spelled differently from a changed file's path is compared after normalisation. Read both changed files in full, then followed the new protocol logic into the report writer, the runner and the gate. Checked each bypass scenario against the code as changed: a step-back round widens the scope to the whole change and keeps every prior id; a forced round demotes a first-round fix to not fixed when the cited evidence is missing; a withdrawn finding must cite a file and line; a path spelled differently from a changed file's path is compared after normalisation. 

</details>

<details><summary>Run details · 3 earlier attempts did not finish</summary>

- small (attempt 1): absent (incomplete): did not read 86 of 146 changed hunks
- small (attempt 2): absent (incomplete): did not read 86 of 146 changed hunks
- small (attempt 3): absent (incomplete): did not read 86 of 146 changed hunks

</details>

<!-- review-verdict: v2 failure aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa -->
<!-- review-protocol: v1 … -->
