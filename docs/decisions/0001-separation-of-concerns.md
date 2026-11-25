# 0001. Separation of concerns between Theseus and the projects it reviews

Status: Accepted, 2026-11-25

## Context

Theseus was extracted from one project, BenchBox, and is now used by BenchBox and by Theseus itself, with a reference caller in `examples/consumer/`. See [PROVENANCE.md](../../PROVENANCE.md). A reviewer that only works well for the project it grew up in is not a reusable reviewer.

The design aims to keep consumer identity out of the package, but it does not fully succeed yet; see the known exceptions below. A consumer pins a release through a lock file (`.github/theseus-lock.json`), which the reusable workflows check against `theseus_ref`. It configures Theseus through a policy file read from the trusted checkout of its gated base, never from the pull request: `bot_login`, `consumer.gated_base`, `consumer.attesters`, the soundness and inert manifests, the classifier gate, logic and data paths, the omp config and pin, and review guidance. It also supplies caller workflow inputs and secrets. See [consumer-policy.md](../consumer-policy.md) and [hosted-workflows.md](../hosted-workflows.md).

The boundary was tested by a concrete proposal. BenchBox's weekly generated data refresh pull requests are too large to review. One option made the Theseus gate verify a BenchBox CI job: a workflow path, a job name and how it is re-triggered. That puts Theseus in the position of judging consumer CI, which the consumer's own branch ruleset already enforces. This record states the principle that rules that option out.

## Decision

Separation of concerns between Theseus and the projects it reviews is a fundamental principle of the library. The reviewer must operate faithfully on any project it is asked to review. These rules make that testable.

1. **Ownership.** The consumer owns its code, data, generators, CI and merge rules. Theseus owns review planning (what must be read), reviewer execution, and when a verdict or gate may clear. Neither side decides the other's matters.
2. **Inputs.** The only inputs Theseus trusts from a consumer are its lock and trusted-base policy, the manifests and files the policy names, the caller workflow inputs and secrets, and commands from the policy's attesters in the forms the policy documents. Theseus's own earlier records, statuses and reviews are also trusted, when they are verified as its own. Everything else in the pull request, including its description, other comments, file names and any file that claims to describe the change, is untrusted data.
3. **Fail closed.** Theseus's safety guarantees hold for any consumer. They may not depend on a consumer's description of itself being complete or honest, beyond what its trusted-base policy declares. When a required declaration is absent or does not parse, Theseus refuses to run. When a declaration does not fit the change, Theseus falls back to full review. It never falls back to less review. A rule such as "reduce review only when every changed file is declared derived" is inside this rule. A rule such as "trust the consumer's list of generator files" is not.
4. **No consumer specifics.** The package and the reusable workflows contain no consumer-specific name, path, job or benchmark. Behaviour that one consumer needs goes in policy, expressed as a general mechanism that any consumer can use.
5. **No consumer CI.** Theseus does not enforce, inspect or re-run consumer CI or branch rulesets, other than the Theseus caller workflows and the statuses its own App posts. Where a clearance relies on an attestation from the consumer, such as an attester's stand-in or adjudication, the gate status says so plainly, and so does the verdict when one exists.
6. **Same rules everywhere.** The reviewer reviews any project it is pointed at by the same rules. Theseus's own repository is a consumer like any other, and nothing in the package or the reusable workflows is keyed to the repository it runs in.

### Applying it

A review of any change that adds a policy key, a manifest, a workflow input or a reviewer behaviour must check it against these rules, and the pull request description should say how. Questions to ask:

- Does it work for a consumer that has never heard of the project that asked for it?
- If the consumer's declaration is missing or wrong, does Theseus read more, not less?
- Can text the author controls (description, comments, a file claiming to describe the change) reduce what is read, other than by the changed files being matched against trusted-base declarations?
- Does it make Theseus depend on, or report on, consumer CI?
- Is any name, path or job in the package or a reusable workflow there only because one consumer uses it?

## Consequences

Positive:

- A new consumer needs a policy file, a lock and secrets, and no change to Theseus.
- Consumer-specific needs are met by general policy mechanisms, which every consumer can use and every release tests.

Negative:

- Some consumer needs, such as very large generated data pull requests, cannot be met by Theseus trusting the consumer. They need a general mechanism that is safe when the consumer's declaration is wrong, and that may be slower or more expensive than a trust-based shortcut. Until one exists, such pull requests are reviewed in full, or, when too large to review, cleared only by an attester's stand-in.
- Each new key needs review against these rules, which takes time.

Known exceptions to resolve. None of these is fixed by this record:

- The review templates `standard.md`, `deep.md` and `gate.md` in `src/theseus_reviewer/templates/` describe every reviewed project as "a SQL benchmarking framework whose published results must be correct" (the opening paragraph). Their defect-target and test-hygiene rules name benchmark results, expected answers, one suite's `allow_network` and "network guard", and `become_subreaper`. This domain text belongs in each consumer's `consumer.review_guidance`. Changing the templates bumps their version and goes through gate review.
- `local.MEMBER_SCHEMA` is `"benchbox_member_review_v1"`, so a consumer's name is in the persisted member record schema. [local-review.md](../local-review.md) documents it as unchanged, and `test_local_review.py` and `test_state_locations.py` assert it. Renaming it changes a persisted format, so it needs a compatibility plan.
- [consumer-policy.md](../consumer-policy.md) names a consumer in general guidance: the review capacity paragraph ("BenchBox's policy can select these reviewers") and the check-run bullet that mentions "the BenchBox digest". Both fall short of the documentation rule below.

Documentation rule: general docs may name a consumer only as an origin note, a dated record, or a clearly marked example. General behaviour is described without naming one. Other docs that name BenchBox do so as origin notes, dated records or marked examples.

Checked and not exceptions: the `theseus_repository` input default in `review.yml` and `gate.yml` is Theseus's own repository, which names Theseus and not a consumer. The `review-eval` workflow's repository condition limits Theseus's own scheduled evaluation to its own repository. It is not a reusable workflow and does not change how any review is planned or judged. The `BenchBox` mentions in `PROVENANCE.md` and `LICENSE` record origin and copyright.

## Rejected alternatives

**The gate verifies a consumer CI job.** Theseus would check that a named workflow and job ran and passed, and that its re-trigger is wired, before clearing a data refresh pull request. Rejected. It makes Theseus judge consumer CI, which the consumer's branch ruleset already enforces. It encodes one consumer's workflow path and job name in a reusable workflow. It breaks when the consumer renames a job.

**Trust a consumer-supplied list of generator files.** A policy would list the generator files, and a pull request that only changes data and those files would get a lighter review. Rejected. The list's completeness and honesty become a safety property of Theseus, and a stale or incomplete list silently reduces review. A pull request can also change a generator and its output in the same commit. The acceptable form is a rule that fails closed: less review only for a change that fits a declaration exactly, and full review for every other change.

**Let a consumer's pull request describe how it should be reviewed.** Rejected because pull request content is untrusted (rule 2). Only the policy on the gated base can change what is read.
