# Provenance

Theseus was extracted from BenchBox (`BenchBox-dev/BenchBox`) at commit `95c423c35efd3fb4962145bdc628006d5aaaad26`.

## History

- Releases v0.1.0 through v0.1.6 were proven to behave identically to the in-tree reviewer that was extracted, by running the same scenarios through both.
- From 0.1.7 on, releases rely on Theseus's own tests and on each consumer's promotion review. No parity run is recorded for them.

## Notices

- `LICENSE` is MIT. It carries the repository copyright and the BenchBox copyright notice of the extracted code (`Copyright (c) 2026 Joe Harris / BenchBox Project`), which the MIT license requires to be retained. The seeded files carry no per-file copyright or third-party license headers (checked with a case-insensitive search for `copyright` and `licen[sc]e` over `src/`).
- The wheel has no runtime dependencies. Its YAML parser is vendored pure Python: PyYAML 6.0.3, `lib/yaml/*.py` from the upstream sdist `https://files.pythonhosted.org/packages/05/8e/961c0007c59b8dd7729d542c61a4d537767a59645b82a0b521206e1e25c2/pyyaml-6.0.3.tar.gz` (sha256 `d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f`, matching PyPI's published digest). The files sit byte-identical under `src/theseus_reviewer/_vendor/yaml/` except that `cyaml.py` (the libyaml binding) is omitted, so no C extension or ambient `yaml` can be loaded. Its MIT license (`Copyright (c) 2017-2021 Ingy döt Net`, `Copyright (c) 2006-2016 Kirill Simonov`) is retained as `src/theseus_reviewer/_vendor/yaml/LICENSE`. `tests/unit/test_vendored_yaml.py` pins the vendored file digest, and ruff excludes `_vendor`.
- OMP is not bundled or redistributed. It is installed from consumer-pinned, hash-verified sources (`install-omp`).

## Frozen source hashes

The sha256 over the sorted lines `<path>\t<sha256 of file bytes>\n` for every tracked file under `src/theseus_reviewer/` at each tag.

| Release | Frozen source hash |
| --- | --- |
| v0.1.0 | `e8aaf67647f3a1bca1d85984c801c6e1c7720b0dd4d6118651c555312cdaab2f` |
| v0.1.1 | `e8aaf67647f3a1bca1d85984c801c6e1c7720b0dd4d6118651c555312cdaab2f` |
| v0.1.2 | `02e24ba5a2768bfc45325d442f06fa5d3e80b8fef7d1adecb2d4b548dd4c4c04` |
| v0.1.3 | `02e24ba5a2768bfc45325d442f06fa5d3e80b8fef7d1adecb2d4b548dd4c4c04` |
| v0.1.4 | `d9454bb1c8f8ccfc192eaebc730ada393774debe61443042878fe467f43e507a` |
| v0.1.5 | `91d8b8da5b387707b149d9f77d83e444b18a807824d6786ac0d76f1ecee4458f` |
| v0.1.6 | `91d8b8da5b387707b149d9f77d83e444b18a807824d6786ac0d76f1ecee4458f` |
| v0.1.7 | `87ed2d9b334c00db085a2ea68397537af2f7bbd9102fa46394f599e55771d8ee` |
| v0.1.8 | `7194dc4c2084bcc248195444b0270ae5ca2d3a95e9b20f7076a959c71af2f531` |
| v0.2.0 | `08f812f29e8338bf3d64387608a8c3e5844dabb0441732f05731eb5079eb2f2f` |
| v0.2.1 | `b08d7bdaffc2f6e2b2484616017cd44289e34f9d2b522090f19dae1880055cba` |
| v0.2.2 | `46ecf8d394358c2fe6f94b5946f73a07005c3a2b54e53c027a897645473b0b91` |
| v0.2.3 | `fa2a8e535658a0ff51f6f871e66e53389263d6ec5c3ed1671753b4c6cf4d762a` |
| v0.2.4 | `fa2a8e535658a0ff51f6f871e66e53389263d6ec5c3ed1671753b4c6cf4d762a` |
| v0.2.5 | `583b86b6f678dcd94206920715fb7796180cb9ffa2fa01f35a71c26551fb0c3b` |
| v0.2.6 | `7476e99f422d69c318fe4d6ee02e74df64d1cb0b023e37ae43b5227bc6a18a31` |
| v0.2.7 | `41ed0c6290765cfe27a3920023dee4d986f35ecd1dd5a6651841f1f1cec79608` |
