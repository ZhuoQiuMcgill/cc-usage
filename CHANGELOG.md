# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
See [VERSIONING.md](VERSIONING.md) for the release policy.

## [Unreleased]

_No unreleased changes yet._

## [2.0.0] - 2026-06-19

First public release: a keyboard-first Claude Code usage TUI, packaged as a
formal, pip-installable tool.

### Added

- **Keyboard-first usage TUI.** Per-model token counts and API-equivalent cost
  across all Claude Code sessions, a usage heartbeat, rolling spend windows
  (1h/5h/24h/7d/all-time), and the official 5h/7d subscription limits captured
  reversibly from the statusline. Operable entirely with arrow keys + Enter.
- **`ccusage` console entry point.** Installs the `ccusage` command (the
  run-from-source launcher matches).
- **Self-update commands.** `ccusage --check-update` reports the installed
  version against the latest GitHub release; `ccusage --update` upgrades to it
  via pip. These are the only commands that touch the network.
- **Single-sourced version.** `cc_usage.__version__` is the one source of truth;
  `pyproject.toml` reads it dynamically. Exposed via `ccusage --version`.
- **Packaging metadata.** MIT license metadata, project URLs, and Python
  classifiers (3.10–3.13).
- **GitHub Actions.** A CI workflow runs the test suite; a Release workflow cuts
  a GitHub Release on each `v*` tag, with PyPI publishing gated on a
  `PYPI_API_TOKEN` secret.

[Unreleased]: https://github.com/ZhuoQiuMcgill/cc-usage/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/ZhuoQiuMcgill/cc-usage/releases/tag/v2.0.0
