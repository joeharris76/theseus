# Bootstrap installer

`theseus bootstrap` (also runnable alone as `python -I -S bootstrap.py ...`, the module is stdlib only) installs Theseus into an isolated, hash-verified environment. The trust chain is: trusted consumer commit, reviewed lock, exact artifact and dependency hashes, byte verification, isolated install, installed-content verification.

```
theseus bootstrap install --repo CONSUMER_CHECKOUT --commit FULL_SHA [--lock-path .github/theseus-lock.json]
                          [--cache-dir DIR] [--entry theseus] [--verify-all-sources] [--reinstall]
theseus bootstrap verify  --repo ... --commit ...     # verify an existing install, create nothing
theseus bootstrap run     --repo ... --commit ... -- ARGS...   # verify, install, exec
```

`install` prints the verified executable path. The cache root is `--cache-dir`, then `THESEUS_INSTALL_CACHE`, then `~/.cache/theseus/installs`.

## Lock

The lock is the format already reviewed by the gate: `.github/theseus-lock.json`, schema 1 (`theseus_reviewer.promotion.parse_lock`). The installer reads these fields: `package`, `version`, `source_commit`, `artifact.sha256`, `artifact.urls` (at least two distinct https URLs: the primary asset and the retained recovery copy) and `dependencies[]` (`name`, `version`, `sha256`, `urls`). It reads the file from the named commit with `git show <sha>:<path>`; the working tree is never consulted.

## What is refused, before any byte runs

- A ref that is not a full 40-hex commit (`latest`, `HEAD`, branch names), a commit that cannot be read, a commit without the lock, a malformed lock.
- An artifact whose bytes do not hash to the locked sha256, from any source that answers. A blocked or unreachable primary falls through to the retained URL, whose bytes must hash identically; a wrong hash from either source refuses, a wrong primary never falls through. `--verify-all-sources` additionally requires every source to answer and match.
- A dependency requirement (`Requires-Dist`, evaluated for this interpreter and platform, including environment markers) that the lock has no hash for, or whose locked version is outside the declared range; a wheel for another platform or an unsatisfied `Requires-Python`.
- A wheel whose `RECORD` does not list exactly its archive entries with matching hashes, symlinks, unsafe paths, `.pth` hooks, `.data` directories, and native binaries in the Theseus wheel itself.

Only the dependency closure reachable from the verified Theseus wheel is fetched and installed. The Theseus wheel has no runtime dependencies (its YAML parser is vendored pure Python), so an empty `dependencies` list is the normal lock and one lock serves every platform and Python 3.11+. The installer still resolves, verifies and installs a closure when a lock names one (for example a locked pure-Python wheel); the lock format carries one sha256 per dependency, so a dependency that is only published as platform wheels would pin one platform per lock.

## Installation and reuse

Wheels are extracted by the installer into `<cache>/installs/<key>/tree/site`, with a launcher in `tree/bin` that runs the locked Python as `python -IBS` with only that site directory on `sys.path`: no user site, no `PYTHONPATH`, no working-directory imports, no bytecode written. The key is the sha256 of the artifact hash, source commit, every locked dependency (name, version, hash), the interpreter identity and the platform.

An install is built in a private staging directory and renamed into place, so an interrupted install leaves nothing usable. Every call re-verifies: cached wheels are re-hashed against the lock (a mismatching cached artifact is discarded and refetched), the expected file set is recomputed from the verified wheels, and the installed tree must equal it byte for byte with no extra files. A corrupt, extended or half-built install is rebuilt from the verified bytes. A warm install whose manifest records a different artifact, dependency set, interpreter or platform than the lock requires is refused until `--reinstall`. A directory or marker alone never proves an install.

## Smoke

`uv build` produces the wheel; the installer test suite (`tests/unit/test_bootstrap.py`) exercises the matrix against synthetic wheels and a loopback mirror (`ORACLE_PROMOTION_MIRROR`, the same loopback convention as `promotion-check`).
