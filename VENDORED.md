# Vendored files

The browser UI must work on a lab network with no internet, so the few
third-party browser files it needs ship inside the package, under
`src/openreactor/static/vendor/`. Python dependencies are never vendored: they
are declared in `pyproject.toml`, pinned in `uv.lock`, and updated by
Dependabot.

## What is vendored

[`src/openreactor/static/vendor/vendor.toml`](src/openreactor/static/vendor/vendor.toml)
lists every file: its package, exact version, licence, the URL it came from,
and its SHA-256. The UI's About page lists the same packages, read from that
manifest. Each package's licence file sits beside it.

## Rules

- **Never edit a vendored file.** openreactor's own styles and scripts go in
  `src/openreactor/static/app.css` and `app.js`, loaded after the vendored
  ones. `tests/test_vendored.py` fails if any file stops matching its recorded
  hash.
- **Updating is a version bump.** Change `version` in the manifest, run
  `just vendor`, and commit the downloaded files with the new hashes it
  records. The diff shows exactly which files changed. Read the package's
  release notes before merging.
- **If a patch ever becomes unavoidable**, it lives as a `.patch` file beside
  the manifest, applied by `scripts/vendor.py`, with the reason recorded here,
  so it is visible and survives the next update.
- **When to stop vendoring:** once the UI needs a build step (TypeScript,
  bundling) or more than a handful of libraries, a JavaScript package manager
  with a lockfile is the better tool. Three prebuilt files do not justify it.

Dependabot cannot see these files, so new releases are not proposed
automatically; check them when touching the UI, and always for a security
advisory against one of them.
