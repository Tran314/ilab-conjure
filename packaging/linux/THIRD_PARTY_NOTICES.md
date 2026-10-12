# Third-party notices

The Linux packages contain iLab CONJURE (GNU AGPLv3, see `LICENSE`), an Astral
python-build-standalone CPython runtime, locked Python dependencies, and prebuilt
JavaScript derived from `package-lock.json`. No Node.js or Rust toolchain ships.

## Python runtime

Exact source release: https://github.com/astral-sh/python-build-standalone/releases/tag/20261009

CPython 3.13.16, x86_64-unknown-linux-gnu, install_only_stripped. The exact asset,
SHA256 and authoritative checksum sources are retained in `python-runtime.json`.
The SHA256 was checked against both the official GitHub release asset digest and
its `SHA256SUMS` file on 2026-10-12; builds verify downloaded and cached bytes before
extraction. Build provenance is not a claim of Ubuntu desktop qualification.

CPython and bundled third-party runtime libraries have their own licenses. License
files supplied by PBS are retained, including `python/lib/python3.13/LICENSE.txt`
and dependency license metadata. The runtime `python/share/` directory is also
retained. Consult
https://docs.python.org/3/license.html and
https://github.com/astral-sh/python-build-standalone for redistribution terms.

## Application dependencies

`app/.deps` retains Python distributions' license metadata; the build records
`python-requirements.lock.txt`. Installation uses the repository's existing
`requirements-webui.txt` with `--require-hashes`. Frontend dependencies and their
versions are in the source repository's `package-lock.json` (including MIT-licensed
Konva and @noble/hashes). Their license texts and package metadata are included
in `third-party-licenses/frontend/`. Review upstream license terms before redistributing modified packages.
GTK3 and Ayatana AppIndicator are system dependencies supplied by Ubuntu and are
not bundled in the archive.
