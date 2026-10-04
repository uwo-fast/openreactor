# Changelog

All notable changes to this project are documented in this file.

The format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Releases before
2.0 are described in their [GitHub releases](https://github.com/uwo-fast/openreactor/releases).

## [Unreleased]

2.0 is a rewrite and is not compatible with 1.x: new configuration, new
database, and new device protocols. 1.x is maintained on the `1.x` branch.

### Added

- Python packaging, linting, type-checking, tests and CI.
- A TOML configuration file that lists every device, validated before
  anything starts: `openreactor check-config <file>`. A commented example is
  in `examples/openreactor.toml`.

### Removed

- The 1.x application. It stays on the `1.x` branch and its tags.
