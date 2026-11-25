# 0001. Separation of concerns between Theseus and the projects it reviews

Status: Accepted, 2026-11-25

## Context

Theseus was extracted from one project, BenchBox, and is now used by several: BenchBox, Theseus itself and the reference caller in `examples/consumer/`. See [PROVENANCE.md](../../PROVENANCE.md). A reviewer that only works well for the project it grew up in is not a reusable reviewer.

The design already keeps consumer identity out of the package. A consumer pins a release through a lock file (`.github/theseus-lock.json`), which the reusable workflows check against `theseus_ref`. It configures Theseus through a policy file read from the trusted checkout of its gated base, never from the pull request: `bot_login`, `consumer.gated_base`, `consumer.attesters`, the soundness and inert manifests, the classifier gate, logic and data paths, the omp config and pin, and review guidance. It also supplies caller workflow inputs and secrets. See [consumer-policy.md](../consumer-policy.md) and [hosted-workflows.md](../hosted-workflows.md).

The boundary was tested by a concrete proposal. BenchBox's weekly generated data refresh pull requests are too large to review. One option made the Theseus gate verify a BenchBox CI job: a workflow path, a job name and how it is re-triggered. That puts Theseus in the position of judging consumer CI, which the consumer's own branch ruleset already enforces. This record states the principle that rules that option out, so it does not have to be rediscovered for each feature.

## Decision

Separation of concerns between Theseus and the projects it reviews is a fundamental principle of the library. The reviewer must operate faithfully on any project it is asked to review. These rules make that testable.

1. **Ownership.** The consumer owns its code, data, generators, CI and merge rules. Theseus owns review planning (what must be read), reviewer execution, and when a verdict or gate may clear. Neither side decides the other's matters.
2. **Inputs.** The only inputs Theseus takes from a consumer are its trusted-base policy and the manifests the policy names, the caller workflow inputs and secrets, and the pull request under review. Pull request content, including its description, comments, file names and any file that claims to describe the change, is untrusted data.
3. **Fail closed.** Theseus's safety guarantees hold for any consumer. They may not depend on a consumer's description of itself being complete or honest, beyond what its trusted-base policy declares. When a declaration is absent, does not parse, or does not fit the change, Theseus falls back to full review and never to less review. A rule such as "reduce review only when every changed file is declared derived" is inside this rule. A rule such as "trust the consumer's list of generator files" is not.
4. **No consumer specifics.** The package and the reusable workflows contain no consumer-specific name, path, job or benchmark. Behaviour that one consumer needs goes in policy, expressed as a general mechanism that any consumer can use.
5. **No consumer CI.** Theseus does not enforce, inspect or re-run consumer CI or branch rulesets. Where a review relies on an attestation from the consumer, such as an attester's stand-in or adjudication, the brief and the verdict say so plainly.
6. **Same rules everywhere.** The reviewer reviews any project it is pointed at by the same rules. Theseus's own repository is a consumer like any other, and no behaviour is keyed to the repository it runs in.

### Applying it

A review of any change that adds a policy key, a manifest, a workflow input or a reviewer behaviour must check it against these rules, and the pull request description should say how. Questions to ask:

- Does it work for a consumer that has never heard of the project that asked for it?
- If the consumer's declaration is missing or wrong, does Theseus read more, not less?
- Does it read anything from the pull request that decides how much of the pull request is read?
- Does it make Theseus depend on, or report on, consumer CI?
- Is any name, path or job in the package or a reusable workflow there only because one consumer uses it?

## Consequences

Positive:

- A new consumer needs a policy file, a lock and secrets, and no change to Theseus.
- A consumer's weak or stale configuration costs it review time, not review quality.
- Consumer-specific needs are met by general policy mechanisms, which every consumer can use and every release tests.
- Theseus can say what it checked and what it took on a consumer's word.

Negative:

- Some consumer needs, such as very large generated data pull requests, cannot be met by Theseus trusting the consumer. They need a general mechanism that is safe when the consumer's declaration is wrong, and that may be slower or more expensive than a trust-based shortcut. Until one exists, such pull requests are reviewed in full or merged by an attester's recorded decision.
- Consumers must maintain their own CI and rulesets, and Theseus will not warn when they drift.
- Each new key needs review against these rules, which takes time.

Known exceptions to resolve. None of these is fixed by this record:

- `src/theseus_reviewer/local.py:81` sets `MEMBER_SCHEMA = "benchbox_member_review_v1"`. The name of one consumer is in the persisted member record schema. [docs/local-review.md](../local-review.md) (line 35) documents it as unchanged, and `tests/unit/reviewer/test_local_review.py:786` and `tests/unit/test_state_locations.py:31` assert it. Renaming it changes a persisted format, so it needs a compatibility plan.

Checked and not exceptions: `.github/workflows/review.yml:18` and `.github/workflows/gate.yml:18` default `theseus_repository` to Theseus's own repository, which names Theseus and not a consumer. `.github/workflows/review-eval.yml:17` limits Theseus's own scheduled evaluation to Theseus's repository. It is not a reusable workflow and does not change how any review is planned or judged. The `BenchBox` mentions in `PROVENANCE.md` and `LICENSE` record origin and copyright.

## Rejected alternatives

**The gate verifies a consumer CI job.** Theseus would check that a named workflow and job ran and passed, and that its re-trigger is wired, before clearing a data refresh pull request. Rejected. It makes Theseus judge consumer CI, which the consumer's branch ruleset already enforces. It encodes one consumer's workflow path and job name in a reusable workflow. It breaks whenever the consumer renames a job, and it gives a second, weaker copy of a check the consumer already owns.

**Trust a consumer-supplied list of generator files.** A policy would list the generator files, and a pull request that only changes data and those files would get a lighter review. Rejected. The list's completeness and honesty become a safety property of Theseus, and a stale or incomplete list silently reduces review. A pull request can also change a generator and its output in the same commit. The acceptable form is a rule that fails closed: less review only for a change that fits a declaration exactly, and full review for every other change.

**Per-consumer branches or flags in the package.** Rejected because they violate rules 4 and 6 and make the reviewer's behaviour depend on who is asking.

**Let a consumer's pull request describe how it should be reviewed.** Rejected because pull request content is untrusted (rule 2). Only the policy on the gated base can change what is read.
