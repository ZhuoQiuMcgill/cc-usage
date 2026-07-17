# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
See [VERSIONING.md](VERSIONING.md) for the release policy.

## [Unreleased]

_No unreleased changes yet._

## [2.4.1] - 2026-07-16

### Fixed

- **Codex subscription limits now read from the rollouts, per account.** The Codex limit
  rows previously depended on the Codex app-server RPC, which fails on recent codex-cli
  builds and — even when it works — only ever reflects the account the local codex CLI is
  logged into. Symptom: the CODEX rows sat at `0% · reset Nd ago · refresh pending` while a
  second, active Codex root (e.g. a Windows `codex_roots` account read from WSL) went
  unrepresented. ccusage now captures the rate-limit snapshot that Codex already embeds in
  each `token_count` rollout event and renders each Codex account's limits from its own
  newest snapshot — labelled `CODEX 5-HOUR`/`CODEX WEEKLY` for a single root, per-account
  (`CODEX-WIN WEEKLY`) once there are several. The snapshot persists in the parse cache and
  survives warm starts. The app-server RPC is still consulted for the default root, where the
  fresher of {RPC, snapshot} wins; its failure is no longer reported as a warning once a
  snapshot covers that account. Limits appear straight after a scan with no network fetch.
  No new network, credentials, or dependencies.

## [2.4.0] - 2026-07-16

### Added

- **Multiple Codex accounts.** Codex transcript discovery now mirrors the Claude model:
  ccusage reads more than one Codex root — always `~/.codex`, plus `$CODEX_HOME` and any
  roots declared under a new `codex_roots` key in `config.json` (`{"path","label","enabled"}`,
  same shape and rules as `claude_roots`) — and treats each root as its own account. Records
  carry a provider (Claude/Codex) distinct from the account label, so a second codex root is
  a first-class account: it gets a scope in the `a` cycle (once there is more than one codex
  root), its own **By account** row, its own settings toggle, and its own per-root parse
  cache/fingerprint. A plain single `~/.codex` machine is byte-for-byte unchanged. This makes
  Windows-side Codex usage visible from WSL: point `codex_roots` at `/mnt/c/Users/<you>/.codex`.
- **Multiple Claude accounts.** ccusage now discovers more than one Claude transcript root
  — always `~/.claude`, plus `$CLAUDE_CONFIG_DIR` and any roots declared under a new
  `claude_roots` key in `config.json` — and treats each config-dir root as one account
  (Claude transcripts carry no account identifier, so the root is the boundary). Roots are
  deduplicated by resolved path, labelled (`~/.claude` → `personal`, others derived from the
  directory name), and missing roots are skipped silently. Every usage record is tagged with
  its account and the persistent parse cache round-trips the tag and pins the active root set.
- **Account scope + by-account rollup.** Press `a` to cycle the panel scope `all` → each
  account → `all`; the scope filters every view (rolling windows, heartbeat, by-model table,
  and the date-range screen), and a specific account excludes Codex. In `all` scope a
  **By account** block shows tokens, cost, and share-of-cost per account (plus a Codex row
  when Codex usage is in-window). A plain single-`~/.claude` setup with no Codex data renders
  byte-for-byte as before — no scope line, no block, `a` inert.
- **Per-account subscription limits.** Each account's 5h/7d limits are fetched from its own
  credentials and the bars are labelled per account (`PERSONAL 5-HOUR`, `RDQCC WEEKLY`).
  One account's fetch failure keeps its last-good values without blocking the others, and the
  reset-time expiry rule applies per account. Single-account limit rendering is unchanged.
- **Settings → Accounts.** A keyboard-driven list of discovered roots (label, provider,
  source, path) with an enable/disable toggle per root, persisted to `config.json`; disabling
  a root excludes its records after the next scan. Adding a new root remains a manual
  `config.json` edit.

### Changed

- **`$CODEX_HOME` now *adds* a Codex root instead of replacing `~/.codex`.** Previously
  setting `CODEX_HOME` made ccusage read only that directory; it is now an additional root
  alongside the always-present `~/.codex` (matching how `$CLAUDE_CONFIG_DIR` behaves for
  Claude). If you relied on `CODEX_HOME` to point ccusage at a single non-default directory,
  both that directory and `~/.codex` are now discovered — disable the one you don't want in
  Settings → Accounts.

## [2.3.0] - 2026-07-13

### Added

- **Unified Claude Code and Codex / ChatGPT usage.** The transcript engine now discovers
  both `~/.claude/projects/**/*.jsonl` and Codex active/archived rollout files, including
  `CODEX_HOME` overrides. Codex cumulative token counters are converted into per-response
  deltas, cached input is split from uncached input, and Claude plus Codex records feed the
  same rolling windows, model tables, activity chart, and date-range analysis.
- **Direct subscription-limit refresh for both providers.** Claude limits are fetched from
  the authenticated read-only usage endpoint with the access token held only in memory;
  expired credentials are refreshed by the installed official Claude client. Codex limits
  are requested from the installed Codex app server over stdio. Provider-scoped windows
  are combined instead of one provider replacing the other, refreshed in the background,
  and retained in a normalized last-good cache that contains no credentials or raw replies.
- **Cold-scan progress and cancellation.** First-time or invalidated scans show byte- and
  file-weighted progress without blocking the TUI. Pressing `c` cancels at a complete JSONL
  boundary, and `r` resumes from the persisted offset without double-counting records.
- **OpenAI pricing capabilities.** The editable price schema now supports explicit cached
  input/output rates and long-context thresholds/multipliers. Bundled standard API rates
  cover the published GPT-5.4, GPT-5.4 mini, GPT-5.5, and GPT-5.6 families while preserving
  the existing Claude rate and ephemeral-cache calculations.
- **Pricing coverage reporting.** Unknown, preview, subscription-only, and local model IDs
  retain all token/activity data while unavailable cost is labelled `unpriced`. Rolling,
  per-model, chart, and date-range views report or mark partial pricing coverage instead of
  presenting unavailable pricing as free usage.

### Changed

- **Immediate warm startup.** Cached aggregates render before filesystem reconciliation;
  transcript discovery, active-to-archive Codex moves, appended-byte parsing, cache writes,
  and live limit refresh continue off the UI thread. On the benchmarked 210,000-record
  local history, cached totals became visible in about 0.43 seconds.
- **Lower cold-scan memory pressure.** Changed rollouts stream through a 4 MiB read buffer
  instead of being copied into memory whole. On the benchmarked 8.15 GB history this cut
  peak private memory from roughly 1.26 GiB to 172 MiB while preserving exact totals,
  line-boundary cancellation, and resume behavior.
- **Adaptive terminal presentation.** Terminals narrower than 76 columns transpose rolling
  windows, collapse model token subcolumns without dropping totals, and wrap scan progress
  into a readable two-line layout. Settings selection and provider-aware labels were also
  tightened for clearer keyboard navigation.
- **Provider-neutral product language and metadata.** CLI help, package metadata, settings,
  file ownership documentation, and panel labels now describe the combined Claude/Codex
  experience. Windows output streams are configured for UTF-8 where supported.
- **No new required runtime dependency.** The install contract remains Python 3.10+ with
  the existing `textual` and `rich` requirements. `orjson` is used only when already
  installed, with the standard-library JSON parser retained as the compatible fallback;
  Rust performance work remains outside the distributed package for this release.

### Removed

- **Statusline capture dependency.** Current installs no longer require or install
  `ccstatusline`, `jq`, a shell wrapper, or Claude `statusLine` configuration. The hidden
  `--restore-statusline` command remains solely as a reversible migration path for users
  who installed the integration through an older ccusage release.

### Fixed

- **Codex model attribution before `turn_context`.** Token events that arrive before a
  rollout's first model marker are buffered as `codex-unattributed`, then reconciled and
  repriced from the authoritative `turn_context`. Pending attribution survives incremental
  scans, cancellation/resume, warm cache reloads, and active-to-archive rollout moves.
- **Codex cache continuity.** Per-rollout model, cumulative-token, pending-attribution, and
  last-observed limit state now round-trip through the persistent cache. Moving a rollout
  from active to archived storage no longer forces a full rebuild when its identity and
  offset remain valid.
- **Provider failure isolation.** A transient Claude or Codex fetch failure keeps that
  provider's last-good limits while allowing the other provider to refresh. Malformed
  replies and credential-refresh failures surface as warnings without crashing the panel.
- **Unpriced activity rendering.** Windows containing real tokens but zero known price no
  longer look empty, and mixed known/unknown totals show the known dollar amount without
  an ambiguous `+ ?` suffix.

### Compatibility

- The persistent parse-cache format advances to include Codex state. An older cache is
  safely ignored and rebuilt once; provider transcripts remain read-only throughout.
- Wheels and source distributions remain pure Python and installable with pip or uv on
  Python 3.10–3.13. Existing configuration and custom pricing files remain supported;
  newly supported pricing fields are optional.

## [2.2.2] - 2026-07-05

### Fixed

- **Stale 5h/7d limit bars after a window resets.** The limit panel renders from the
  last statusline capture, which only refreshes when a Claude Code turn fires. If a
  window reset while nothing was running (e.g. you hit 85%, closed everything overnight,
  and the 5-hour window rolled over), the panel kept showing the old **85%** until the
  next Claude Code turn wrote a fresh capture. The bars are now evaluated against the
  current time on every refresh tick: once a bucket's `resets_at` moment has passed, it
  renders **0%** with an empty bar and a dim `reset <duration> ago · awaiting next turn`
  note instead of the stale percentage — no fabricated next-window countdown, since the
  next window only begins on your next turn. Each bucket is judged independently (the 5h
  row can read expired while the weekly row is still counting down), and the row flips
  live the first tick after the reset time.

## [2.2.1] - 2026-07-03

### Changed

- **Release notes now mirror the changelog.** The Release workflow builds each GitHub
  Release body from that version's `## [X.Y.Z]` section in `CHANGELOG.md` (with GitHub's
  auto "What's Changed" appended), instead of a bare auto-generated PR-title list — so
  published release notes follow the same Keep a Changelog convention as this file.

### Fixed

- **Streaming-usage undercount in the transcript parser.** Claude Code writes
  several transcript lines for a single streaming assistant reply — all sharing one
  `(requestId, message.id)` — where only `output_tokens` grows across them (the
  first line is a partial `message_start` snapshot of ~1–7 output tokens; the last
  carries the final counts). The parser deduped by that key and kept the **first**
  line, throwing away the final output count — the most expensive tokens — so cost
  and output tokens were systematically **undercounted**, severely so for
  agent-heavy sessions (on one real 103-agent workflow session, output tokens were
  undercounted by more than an order of magnitude and reported cost was roughly half
  of actual). Repeat lines are now **merged** into the kept record: field-wise max
  of every token counter, cost recomputed from the merged counts, and the first
  line's timestamp preserved so the record never shifts time bucket. The merge holds
  across incremental scans and warm starts from the persistent parse cache (whose
  format version was bumped, so any older cache is rebuilt once). **Historical totals
  will increase** after this fix — they were undercounted before, and the higher
  numbers are the correct ones.

## [2.2.0] - 2026-07-01

### Added

- **Persistent parse cache for near-instant warm startup.** The incremental parse state
  (per-file offsets + extracted records + the dedup set) is now saved to
  `~/.config/cc-usage/parse-cache.pkl` and reloaded on the next launch, so a relaunch
  re-parses only transcript bytes appended since last time instead of the entire corpus.
  On a real ~3 GB / 6k-file history this took cold startup from ~7 s to under 1 s (~10×),
  with warm-start totals byte-identical to a cold scan. The cache is pure derived data:
  it's invalidated automatically on a pricing-table change, a deleted transcript, or a
  format-version bump, and a missing/corrupt/incompatible cache degrades silently to a
  full scan (never a crash). Safe to delete at any time.

### Changed

- **Startup no longer blocks the UI.** The first (potentially multi-second) transcript
  scan now runs in a background worker thread: the panel paints immediately with a brief
  `scanning transcripts…` placeholder and fills in when the scan completes, instead of
  freezing until it's done.
- **Optional `orjson` acceleration.** If the [`orjson`](https://github.com/ijl/orjson)
  package is present it's used to parse transcript JSON (~2× faster on this workload);
  it is not a dependency and the stdlib `json` path (with tolerant UTF-8 decode) is used
  otherwise. This only affects the rare cold scan — warm starts read almost nothing.

## [2.1.4] - 2026-07-01

### Fixed

- **`--update` could silently no-op on a pinned `uv tool` install.** The v2.1.3 uv-tool
  fix routed the plain `--update` path through a bare `uv tool upgrade`, which re-resolves
  against whatever source the tool is *currently* recorded as using. If that install was
  ever pinned to a specific rev — by a prior `--update-pr` / `--update-prerelease` /
  `--update-stable` call, or by a user following the README's own "pin a specific release"
  instructions — `uv tool upgrade` silently exits 0 with "Nothing to upgrade" even when a
  newer release exists, so `ccusage --update` would falsely report success while actually
  changing nothing. The uv backend now always force-installs the freshly resolved release
  tag explicitly (`uv tool install --force git+...@<tag>`), the same way the force-reinstall
  commands already did, regardless of any existing pin. The README's own "pin a release,
  then `uv tool upgrade` later" advice had the same bug and is corrected too.

## [2.1.3] - 2026-07-01

### Fixed

- **`--update*` now works on a `uv tool` install.** The built-in self-update commands
  (`--update`, `--update-pr`, `--update-prerelease`, `--update-stable`) always shelled out
  to `pip`, which a `uv tool install` environment doesn't have — so there they failed with
  a generic "could not run pip" instead of upgrading anything. The updater now detects a
  pip-less environment with `uv` on `PATH` and routes through `uv tool upgrade` (plain
  `--update`) or `uv tool install --force git+...@<ref>` (the force-reinstall commands)
  instead, matching the commands the README already told uv users to run by hand.
- **A clear message when Windows blocks replacing ccusage's own running executable.**
  Upgrading ccusage while ccusage itself drives the upgrade means its own launcher `.exe`
  is open; Windows (unlike Unix) refuses to overwrite a running executable's image, so
  pip/uv can install the new package but the final entry-point step fails with a bare
  Win32 "being used by another process" error. The updater now recognizes that specific
  failure and prints a clear hint instead: the package is usually updated already, and
  closing other running `ccusage` windows (or running the update once via
  `python -m cc_usage` instead of `ccusage`) refreshes the command too.

## [2.1.2] - 2026-07-01

### Added

- **Claude Sonnet 5 pricing.** `claude-sonnet-5` is now in the bundled pricing
  table ($3 input / $15 output per 1M tokens), so its usage is costed instead of
  flagged unknown ($0). Cache read/write costs derive from the input rate as
  usual, and the id is matched tolerantly (`[1m]` / date suffixes stripped).
  Sonnet 5's introductory $2/$10 rate (through 2026-08-31) can be set in the
  user-editable `pricing.json` if you prefer the promo rate.

## [2.1.1] - 2026-06-21

### Changed

- **Docs:** the README now recommends installing with **uv** (`uv tool install`),
  keeping pipx and pip-in-a-venv as alternatives, and clarifies that the built-in
  `ccusage --update*` commands need a pip-bearing environment — so `uv tool` installs
  upgrade with `uv tool upgrade cc-usage` instead.

## [2.1.0] - 2026-06-21

### Added

- **Date-range usage analysis.** A keyboard-only analysis screen (open with `d`)
  showing usage metrics for an arbitrary inclusive start → end calendar range:
  totals (tokens in/out/cache, API-equivalent cost, active days, record count), a
  by-model table, a by-day breakdown, and a daily braille chart. Pick the range
  with arrow keys — a preset picker (last 7/30 days, this/last month, all time,
  custom) and a date stepper (↑/↓ ±1 day, ←/→ ±1 month, PgUp/PgDn ±1 year); day
  boundaries use the local calendar.
- **Test-channel self-update commands.** Install unreleased / test builds from the
  CLI and return cleanly to the official release:
  - `ccusage --update-pr <N>` force-reinstalls the head of **open PR #N**
    (`refs/pull/<N>/head`) for testing — the only way to reach unmerged code. It
    cautions that the code is unreviewed and prints how to return to stable.
  - `ccusage --update-prerelease` force-reinstalls the newest prerelease GitHub
    release, falling back to `@main` when none is published.
  - `ccusage --update-stable` force-reinstalls the latest stable release tag (the
    return-to-official path).
  - `ccusage --check-prerelease` reports the installed version against the latest
    prerelease tag and installs nothing.

  Like the existing `--update` / `--check-update`, these touch the network only as
  explicit user actions, need no credentials, and are never reached from the
  panel/data path. Force-reinstall is used because feature/test builds do not bump
  `__version__`. README and VERSIONING.md document the commands and a one-time
  manual bootstrap (a stable build doesn't yet have the `--update-*` flags).

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

[Unreleased]: https://github.com/ZhuoQiuMcgill/cc-usage/compare/v2.4.1...HEAD
[2.4.1]: https://github.com/ZhuoQiuMcgill/cc-usage/compare/v2.4.0...v2.4.1
[2.4.0]: https://github.com/ZhuoQiuMcgill/cc-usage/compare/v2.3.0...v2.4.0
[2.3.0]: https://github.com/ZhuoQiuMcgill/cc-usage/compare/v2.2.2...v2.3.0
[2.2.2]: https://github.com/ZhuoQiuMcgill/cc-usage/compare/v2.2.1...v2.2.2
[2.2.1]: https://github.com/ZhuoQiuMcgill/cc-usage/compare/v2.2.0...v2.2.1
[2.2.0]: https://github.com/ZhuoQiuMcgill/cc-usage/compare/v2.1.4...v2.2.0
[2.1.4]: https://github.com/ZhuoQiuMcgill/cc-usage/compare/v2.1.3...v2.1.4
[2.1.3]: https://github.com/ZhuoQiuMcgill/cc-usage/compare/v2.1.2...v2.1.3
[2.1.2]: https://github.com/ZhuoQiuMcgill/cc-usage/compare/v2.1.1...v2.1.2
[2.1.1]: https://github.com/ZhuoQiuMcgill/cc-usage/compare/v2.1.0...v2.1.1
[2.1.0]: https://github.com/ZhuoQiuMcgill/cc-usage/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/ZhuoQiuMcgill/cc-usage/releases/tag/v2.0.0
