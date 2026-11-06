# Third-party material and dependency notices

Project-owned code is Apache-2.0, Copyright 2026 jiaoenqi. This does not relicense third-party material.

## Included source and evaluation evidence

- `docs/evidence/r23-20261010/upstream-node-0.26.0.c` and `upstream-point-0.26.0.c` are py-tree-sitter 0.26.0 source snapshots. Their MIT notice, Copyright (c) 2019 Max Brunsfeld, GitHub, is preserved in `docs/evidence/r23-20261010/upstream-LICENSE.txt`.
- Synthetic benchmark tasks and mini repositories are project-authored development fixtures. Their labels are not real-world performance evidence.
- Historical evaluation records and patches refer to external repositories. [The upstream manifest](upstream/manifest.json) records 65 repository/base-commit pairs, exact license-source URLs, SHA-256 digests and 25 deduplicated license texts. These notices accompany excerpts and patch context; upstream code is not covered by the project's Apache-2.0 grant.
- The frozen SWE-bench environment-specification snapshot is derived from SWE-bench 4.1.0. Its [MIT license](upstream/SWE-bench--v4.1.0.txt) is preserved from the official `v4.1.0` tag. Full downloaded datasets and external repository caches are not part of the release allowlist.
- The manifest also records referenced projects whose code is not bundled. In particular, Pylint references in tracked catalogs and environment metadata do not include a Pylint source patch; its GPL notice does not imply that the independent project code has been relicensed GPL.
- `NOASSERTION` in GitHub's license metadata means automated classification was inconclusive. The retrieved full text is authoritative for that upstream material; it does not mean the material has no license.

## Frontend

The frontend's complete production dependency license texts, including Inter and JetBrains Mono under SIL OFL-1.1, are in [web/public/third-party-notices.txt](../../web/public/third-party-notices.txt). Vite copies this file to `/app/third-party-notices.txt` in the built dashboard. Keep this file when redistributing the frontend build.

The dependency inventory includes MIT, ISC, BSD, Apache-2.0, OFL-1.1, CC-BY-4.0 and other licenses. Do not assume every dependency uses Apache-2.0. The application screenshots in `docs/assets/phase15` are project UI captures; README describes their offline demonstration provenance.

## Python and binary distribution

[python-dependencies.json](python-dependencies.json) records license metadata for all registry packages in the universal lockfile, including optional/platform-specific dependencies. The [Python SBOM](python-sbom.cdx.json) and [frontend SBOM](web-sbom.cdx.json) describe the locked dependency graphs, not proof that every optional dependency was installed or tested.

The project source and wheel do not vendor third-party Python wheels, CUDA libraries, or downloaded models. Optional PostgreSQL support includes LGPL-licensed psycopg; optional GPU embedding dependencies include NVIDIA proprietary licenses. Redistributors who bundle those binaries or build container images must preserve their license files and comply with their separate terms. A model's own license must also be checked before redistributing its weights.

License metadata is an inventory, not a blanket clearance for arbitrary future code, datasets, models or binary bundles. Keep new material's provenance and notices with each contribution.
