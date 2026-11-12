# Soundness-path matcher

`theseus_reviewer/soundness_paths.py` is one stdlib-only, import-pure module. It performs no file access at import and never imports the rest of the package, so a consumer can vendor a single byte-identical, hash-pinned copy and run it from a trusted git ref under `python -I -S`.

```
python -I -S soundness_paths.py --manifest PATH [--manifest PATH ...] (--stdin | --paths-file FILE) [--format plain|shell|github-output]
python -I -S soundness_paths.py --print-source-sha256
```

- There is no default manifest: `--manifest` is required.
- Several manifests combine as a union. Pass the base and the pull request's manifest to guarantee that a changed manifest cannot narrow the tier a path requires; an unreadable manifest is an error, not an empty match.
- Manifest lines are `kind<TAB>pattern` with kinds `file`, `glob`, `prefix` (must end in `/`) and `regex`; `#` starts a comment.

## Vendoring and drift

The owner-approved arrangement is a single verbatim copy in each consumer, recorded in the lock's `matcher` entry (`path`, `sha256`) and updated only by promotion pull requests. Drift is checked against the installed release:

```
theseus matcher verify --copy PATH_TO_VENDORED_COPY [--sha256 PINNED_DIGEST]
```

It exits 1 when the copy differs from the installed matcher or from the pinned digest. `theseus matcher digest` prints the installed module's sha256. In-package callers use `theseus_reviewer.matchers.load_matchers(consumer, root)`, which loads the soundness manifest, the optional inert manifest and the optional reviewer guidance file named in the consumer policy; an invalid guidance file raises `PolicyError`.
