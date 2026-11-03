# Security Policy

## Reporting

Report vulnerabilities privately through GitHub private vulnerability reporting on this repository (Security tab, "Report a vulnerability"). If that is unavailable, contact the owner `@joeharris76` through the address on their GitHub profile. Do not file public issues or pull requests containing exploit details.

## Scope

Theseus is a merge gate: issues that let a pull request author bypass review or the gate, forge or replay a verdict, read or exfiltrate credentials, or make the installer execute unverified bytes are in scope.

## Trust model

- Candidate code (fork PRs, unmerged heads, candidate builds) is untrusted and never receives App, tailnet, trusted-publisher or other secret-bearing environment access.
- Gate identity, attesters, gated branch and policy paths come from trusted consumer policy on the gated branch; the package carries no defaults.
- Installers verify artifact and dependency hashes before executing any bytes.
- Until a separately authenticated Theseus oracle gate is live, merges require owner full-diff review.

## Supported versions

Only the latest tagged release and `main` receive fixes.
