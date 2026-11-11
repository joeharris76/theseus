### 🔧 SHIP WITH FIXES · 6 findings block merge

**5 High · 1 Medium** · reviewed aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa · fix each one, or contest it with a comment `Dispute: D<n> - <reason>`

<details><summary>🟠 <b>High</b> · D2 · Detect shared-head collisions independently of the local base policy · <code>benchbox/core/equivalence/other.py:204-206</code></summary>

The collision check runs only when this branch's policy lists several bases, and the rival-base lookup additionally ignores bases absent from that local policy, so a pull request whose head is shared with another base is judged against the wrong policy. The consequence is that a gate can pass on a head that another protected branch would have refused, and the second branch never sees the first branch's findings. Detect the collision before consulting the local policy, and refuse when the shared head has no review on every base that contains it.

</details>

<details><summary>🟠 <b>High</b> · D3 · Do not manufacture a maintained disposition from a fixed answer · <code>benchbox/core/equivalence/other.py:1107-1109</code></summary>

The collision check runs only when this branch's policy lists several bases, and the rival-base lookup additionally ignores bases absent from that local policy, so a pull request whose head is shared with another base is judged against the wrong policy. The consequence is that a gate can pass on a head that another protected branch would have refused, and the second branch never sees the first branch's findings. Detect the collision before consulting the local policy, and refuse when the shared head has no review on every base that contains it.

</details>

<details><summary>🟠 <b>High</b> · D4 · Unchanged finding lines do not verify pre-existing behavior · <code>benchbox/core/equivalence/other.py:858-860</code></summary>

The collision check runs only when this branch's policy lists several bases, and the rival-base lookup additionally ignores bases absent from that local policy, so a pull request whose head is shared with another base is judged against the wrong policy. The consequence is that a gate can pass on a head that another protected branch would have refused, and the second branch never sees the first branch's findings. Detect the collision before consulting the local policy, and refuse when the shared head has no review on every base that contains it.

</details>

<details><summary>🟠 <b>High</b> · D5 · Bind accepted live results to trusted execution evidence · <code>benchbox/core/equivalence/other.py:677-679</code></summary>

The collision check runs only when this branch's policy lists several bases, and the rival-base lookup additionally ignores bases absent from that local policy, so a pull request whose head is shared with another base is judged against the wrong policy. The consequence is that a gate can pass on a head that another protected branch would have refused, and the second branch never sees the first branch's findings. Detect the collision before consulting the local policy, and refuse when the shared head has no review on every base that contains it.

</details>

<details><summary>🟠 <b>High</b> · D6 · Include gate, scorer and corpus changes in evaluation enforcement · <code>benchbox/core/equivalence/other.py:640-642</code></summary>

The collision check runs only when this branch's policy lists several bases, and the rival-base lookup additionally ignores bases absent from that local policy, so a pull request whose head is shared with another base is judged against the wrong policy. The consequence is that a gate can pass on a head that another protected branch would have refused, and the second branch never sees the first branch's findings. Detect the collision before consulting the local policy, and refuse when the shared head has no review on every base that contains it.

</details>

<details><summary>🟡 <b>Medium</b> · D7 · Do not report a passing evaluation gate without a baseline · <code>benchbox/core/equivalence/other.py:653-655</code></summary>

The collision check runs only when this branch's policy lists several bases, and the rival-base lookup additionally ignores bases absent from that local policy, so a pull request whose head is shared with another base is judged against the wrong policy. The consequence is that a gate can pass on a head that another protected branch would have refused, and the second branch never sees the first branch's findings. Detect the collision before consulting the local policy, and refuse when the shared head has no review on every base that contains it.

</details>

<details><summary>Reviewer notes · <code>small (omp @smol)</code></summary>

Reviewer: `small (role review, omp @smol)`.

Read both changed files in full, then followed the new protocol logic into the report writer, the runner and the gate. Checked each bypass scenario against the code as changed: a step-back round widens the scope to the whole change and keeps every prior id; a forced round demotes a first-round fix to not fixed when the cited evidence is missing; a withdrawn finding must cite a file and line; a path spelled differently from a changed file's path is compared after normalisation. Read both changed files in full, then followed the new protocol logic into the report writer, the runner and the gate. Checked each bypass scenario against the code as changed: a step-back round widens the scope to the whole change and keeps every prior id; a forced round demotes a first-round fix to not fixed when the cited evidence is missing; a withdrawn finding must cite a file and line; a path spelled differently from a changed file's path is compared after normalisation. 

</details>

<!-- review-verdict: v2 failure aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa -->
<!-- review-protocol: v1 … -->
