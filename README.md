# ccusage

A keyboard-first, interactive terminal app for your **Claude Code and Codex / ChatGPT app**
usage across **all** local sessions — per-model token counts, API-equivalent dollar cost,
a compact usage **heartbeat**, rolling spend windows, and locally reported subscription
limits fetched directly from both providers.

**One command launches it; you drive everything — viewing, switching views, and all
configuration — with arrow keys + Enter. There are no flags to memorize.**

```
CC Usage
─────────────────────────────────────────────────────────────────────

5-HOUR   ▓▓░░░░░░░░░░░░   1%  resets in 3h14m
WEEKLY   ▓▓░░░░░░░░░░░░  13%  resets in 5d03h
─────────────────────────────────────────────────────────────────────

Usage      1h      5h      24h       7d   all-time
tokens   9.2M   51.0M   128.0M   402.0M       3.7B
cost    $8.40  $52.10  $131.50  $455.00  $4,120.00
─────────────────────────────────────────────────────────────────────

Activity cost · 24h   (left/right = window · up/down = metric)
$12.40 ⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⡇⠀⠀⠀⠀⠀⢠⠀⠀⠀
       ⠀⠀⠀⠀⠀⠀⠀⠀⠀⡆⢸⠀⠀⠀⣿⡆⢸⡇⠀⠀⣸⠀⠀⠀
 $6.20 ⠀⠀⠀⠀⠀⠀⠀⠀⠀⡇⢸⠀⠀⠀⣿⡇⣿⡇⠀⠀⣿⢰⡇⠀
 $4.10 ⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣸⠀⠀⠀⣿⡇⣿⡇⠀⠀⣿⢸⡇⢸
     0 ⣀⣀⣀⣀⣀⣀⣀⣀⣠⣿⣿⣀⣀⣀⣿⣷⣿⣿⣠⣇⣿⣿⣇⣸
       -24h  -18h  -12h  -6h  now
       peak $12.40/bucket · 14:30 (3h ago)
─────────────────────────────────────────────────────────────────────

Models · all-time
Model        In    Out   Cache       Cost
Opus 4.8   2.6M  18.0M    2.7B  $2,600.00
Fable 5    610K   4.6M  920.0M  $1,510.00
Haiku 4.5   30K   210K   45.0M      $9.99
Total      3.2M  22.8M    3.7B  $4,120.00
─────────────────────────────────────────────────────────────────────

cost = API-equivalent value of tokens · you are on a subscription
```

*Illustrative mock with scrubbed numbers — the live panel is a Textual TUI; braille
heights, axis labels, and timers update in place. On GitHub the heartbeat braille may not
render at exact monospace width; in a real terminal the columns line up.*

## Install

Requires **Python 3.10+**. There are no required shell utilities or external statusline
packages. Any installer below puts the `ccusage` command on your `PATH` — **uv is
recommended.**

### With uv (recommended)

[uv](https://docs.astral.sh/uv/) installs `ccusage` into its own isolated environment, so it
works even on modern **externally-managed** Pythons (PEP 668) where a system-wide
`pip install` is blocked:

```bash
uv tool install "git+https://github.com/ZhuoQiuMcgill/cc-usage.git"
ccusage
```

Pin a specific release by appending a tag (`…cc-usage.git@v2.1.0`). A pin locks the exact
git rev, so plain `uv tool upgrade cc-usage` won't move it forward later (it re-resolves
the same pinned rev and reports nothing to do) — upgrade with `ccusage --update` instead
(it force-installs the new release explicitly, regardless of any existing pin), or
re-pin by hand with `uv tool install --force "git+…@vX.Y.Z"`.

### With pipx

[pipx](https://pipx.pypa.io/) is an equally good isolated install:

```bash
pipx install "git+https://github.com/ZhuoQiuMcgill/cc-usage.git"
ccusage
```

Upgrade with `pipx upgrade cc-usage`.

### With pip (in a virtual environment)

A plain `pip install` works **inside a virtual environment** (a system-wide `pip install`
is blocked on PEP 668 / externally-managed Pythons):

```bash
python3 -m venv ~/.venvs/ccusage
~/.venvs/ccusage/bin/pip install "git+https://github.com/ZhuoQiuMcgill/cc-usage.git"
~/.venvs/ccusage/bin/ccusage
```

(Once published to PyPI you'll be able to install `cc-usage` by name instead of the git URL.)

### Staying up to date

The simplest upgrade is your installer's own command:

```bash
uv tool upgrade cc-usage      # if you installed with uv (unless you ever pinned a tag — see below)
pipx upgrade cc-usage         # if you installed with pipx
```

`ccusage` also has **built-in** self-update commands that fetch the latest GitHub release:

```bash
ccusage --check-update   # report current vs latest release; install nothing
ccusage --update         # upgrade to the latest release
ccusage --version        # print the installed version
```

> The built-in `--update*` commands work on **every** install method. On pipx / venv
> installs they shell out to `pip`. A **uv tool** environment has no `pip` — there they
> detect that automatically and shell out to `uv tool install --force "git+…@<resolved
> tag>"` instead, so no manual step is needed. This always targets the freshly resolved
> release explicitly (never a bare `uv tool upgrade`), so it works even if the install was
> previously pinned to an older tag by `--update-pr` / `--update-prerelease` /
> `--update-stable`, or by hand — cases where plain `uv tool upgrade cc-usage` silently
> has nothing to do.

> **Windows:** running any `--update*` command from the same `ccusage` you're updating
> can't replace that running `.exe` — Windows refuses to overwrite the image of a running
> executable (Unix has no such restriction). ccusage detects this and tells you the
> package is usually updated anyway (confirm with `ccusage --version`); to refresh the
> command too, close other running `ccusage` windows and re-run, or run it once via
> `python -m cc_usage` in place of `ccusage`.

`--check-update` / `--update` are explicit user actions; the panel itself never makes a
network call.

#### Testing unreleased builds (test channel)

Sometimes you want to try work that isn't merged or released yet — most often the code on
an **open pull request**, before deciding to merge it. These commands install a **test
build** and give you a clean way back to the official release:

```bash
ccusage --update-pr <N>      # install the head of open PR #N for testing
ccusage --update-prerelease  # install the latest prerelease build (or @main if none)
ccusage --check-prerelease   # report current vs latest prerelease tag (installs nothing)
ccusage --update-stable      # return to the latest official release
```

`--update-pr`, `--update-prerelease`, and `--update-stable` always force a reinstall
(pip's `--force-reinstall`, or `uv tool install --force` on a uv tool install), because a
test build and the stable build can share the same version string — without it, the
installer would think you're already up to date and not switch.

> **`--update-pr <N>` installs unreviewed code from a pull request.** Only do it for a PR
> you trust, and return to the official release with `ccusage --update-stable` when you're
> done.

**One-time bootstrap.** A machine on a stable build doesn't have the `--update-*` commands
yet (they ship *with* the test build), so the **first** install of a test build uses your
installer directly — for an open PR #N:

```bash
uv tool install --force "git+https://github.com/ZhuoQiuMcgill/cc-usage.git@refs/pull/<N>/head"   # uv
pipx install --force "git+https://github.com/ZhuoQiuMcgill/cc-usage.git@refs/pull/<N>/head"      # pipx
```

After that the in-app `ccusage --update-pr`, `--update-prerelease`, `--update-stable`, and
`--check-prerelease` commands are available on **every** install method, including uv tool —
they detect the missing `pip` there and shell out to the equivalent `uv tool install --force`
command automatically.

All of the update commands above reach the network **only** as explicit user actions; the
passive panel/data path stays strictly no-network.

### Run from source (development)

To hack on it, install editable into a venv:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .        # installs textual + rich, pinned
```

You can run it with the bundled launcher (no venv activation needed):

```bash
./ccusage
```

To call it from anywhere, symlink the launcher onto your `PATH`:

```bash
ln -s "$PWD/ccusage" ~/.local/bin/ccusage   # then just: ccusage
```

(`pip install -e .` also creates a `ccusage` entry point inside `.venv/bin/`.)

## Usage — keyboard only

Just run `ccusage`. The whole app is operable with **arrow keys + Enter** (plus a quit
key). No flag is required for anything.

| Key | What it does |
|---|---|
| **← / →** | Switch the **heartbeat window** — 5h / 24h / 7d (default 24h) |
| **↑ / ↓** | Toggle the heartbeat **metric** — cost / tokens (`t` also works, as a shortcut) |
| **s** or **Enter** | Open **Settings** (refresh interval, default table window, show-cost, theme) |
| **a** | Cycle the **account scope** — all → each account → all (only with more than one Claude or Codex account; see [Multiple accounts](#multiple-accounts)) |
| **↑ / ↓** | Move the selection inside Settings and its pickers; **Enter** confirms |
| **Esc** | Back out of Settings / a picker |
| **c** | Cancel a cold transcript scan; **r** resumes it from the last completed line |
| **q** or **Ctrl-C** | Quit cleanly — the terminal is restored |

Data keeps refreshing on the configured interval while the UI stays responsive; the reset
timers and the heartbeat update live.

### The only flags (none required)

| Command | What it does |
|---|---|
| `ccusage` | Launch the interactive TUI (default). |
| `ccusage --once` | Print a single static frame and exit (handy for scripts). |
| `ccusage --check-update` | Report your version vs the latest GitHub release (installs nothing). |
| `ccusage --update` | Upgrade to the latest release via pip. |
| `ccusage --update-pr <N>` | Install the head of open PR #N for testing (force-reinstall; **unreviewed code**). |
| `ccusage --update-prerelease` | Install the latest prerelease build (or `@main`) for testing (force-reinstall). |
| `ccusage --update-stable` | Return to the latest official release (force-reinstall). |
| `ccusage --check-prerelease` | Report your version vs the latest prerelease tag (installs nothing). |
| `ccusage --version` / `--help` | Version / usage. |

Older releases could install a Claude statusline integration. Current releases never
install or depend on one. If you used that older feature, run the hidden one-time cleanup
command `ccusage --restore-statusline`.

## The heartbeat

A fixed **~8-row braille chart with real axes** that spikes when you're active and goes
flat when idle. Each column is the **usage rate per time bucket** — the sum of cost (or
tokens) for the records in that bucket, **not** a cumulative running total.

- **Y-axis (dynamic, peak = top):** the scale is `[0 … peak]`, where `peak` is the largest
  bucket value in the current window+metric, so the tallest column always reaches the top
  row. The left gutter labels the peak (top), `0` (bottom) and a couple of mid ticks,
  formatted per metric (cost `$X.XX`, tokens `K`/`M`). It rescales every refresh.
- **X-axis (time):** a few evenly spaced time ticks run beneath the chart, scaled to the
  window (`-24h … now`, or days for 7d), and a **peak-time annotation** tells you *when*
  the peak happened — e.g. `peak $12.40/bucket · 14:30 (3h ago)` (clock time + relative,
  from the peak bucket's center time).

Switch the window (5h / 24h / 7d) with **← / →** and toggle the metric (cost / tokens)
with **↑ / ↓** (`t` is also a shortcut). An idle window shows a flat baseline at 0 with the
axes still labeled and `no activity`.

## Settings (in-app, keyboard-driven)

Press **s** (or **Enter**) on the panel. Every value is chosen from a list — never typed:

- **Refresh interval** — 2s / 5s / 10s / 30s
- **Default table window** — all-time / 1h / 5h / 24h / **7d**
- **Show cost column** — on / off
- **Theme** — dark / light / high-contrast
- **Accounts** — enable/disable discovered Claude and Codex account roots (shown only when
  you have more than one root for either provider; see [Multiple accounts](#multiple-accounts))

Choices persist to `~/.config/cc-usage/config.json` and apply live.

## Subscription limits

Both providers are fetched automatically in the background and shown together with
provider-tagged labels. A normalized last-good snapshot is cached so startup remains
fast and temporary network or rate-limit errors do not blank the panel.

- **Codex / ChatGPT:** ccusage starts the installed Codex app server and calls its
  documented `account/rateLimits/read` RPC. Codex owns authentication and token refresh;
  ccusage never reads Codex credentials.
- **Claude Code:** ccusage reads the access token from Claude Code's local credential
  file in memory and calls Anthropic's read-only OAuth usage endpoint. If the token has
  expired, credential renewal is delegated to the official `claude` executable using an
  empty zero-turn invocation. Token values and raw responses are never logged or cached.

Limits refresh after the initial transcript scan and then every five minutes. On a warm
start, cached transcript totals and cached limits render immediately while reconciliation
and live fetching continue off the UI thread.

There is no `ccstatusline`, `jq`, shell wrapper, or statusline configuration dependency.
The hidden `ccusage --restore-statusline` command exists only to undo integrations
installed by older ccusage versions.

## Multiple accounts

If you run more than one Claude or Codex account on a machine — a personal one under
`~/.claude` and a company one under a separate config dir, or Codex on both WSL and the
Windows side — ccusage can show them side by side. Neither Claude transcripts nor Codex
rollouts carry an **account identifier**, so the provider **config-dir root** is the account
boundary; each root is treated as one account. Provider stays a separate dimension: a Codex
root is a Codex account (its limits and pricing are unchanged), it just gets its own label,
scope, and rollup row like a Claude account.

**How Claude roots are discovered** (deduplicated by resolved path, all read-only):

1. `~/.claude` — always (labelled `personal`).
2. `$CLAUDE_CONFIG_DIR` — when set and different from `~/.claude`.
3. `config.json` → `claude_roots` — a list of extra roots.

**Codex roots mirror this exactly**:

1. `~/.codex` — always (labelled `codex`).
2. `$CODEX_HOME` — when set and different from `~/.codex`. Note this now *adds* a root
   (it used to replace `~/.codex`).
3. `config.json` → `codex_roots` — a list of extra roots, same
   `{ "path", "label", "enabled" }` shape as `claude_roots`.

The common setup is the isolated-config-dir alias:

```bash
alias claude-rdqcc='CLAUDE_CONFIG_DIR="$HOME/.claude-rdqcc" claude'
```

Run ccusage with that same `CLAUDE_CONFIG_DIR` exported and both accounts are discovered.
To have ccusage *always* see the second account regardless of environment, declare it in
`~/.config/cc-usage/config.json` instead (adding a root is a manual edit):

```json
{
  "claude_roots": [
    { "path": "~/.claude-rdqcc", "label": "rdqcc", "enabled": true }
  ]
}
```

`path` is `~`-expanded; `label` and `enabled` are optional. A missing root is skipped
silently. **Labels** default to `personal` for `~/.claude` and `codex` for `~/.codex`; any
other root derives from the directory basename with a leading `.claude-` / `.codex-` / `.`
stripped (`~/.claude-rdqcc` → `rdqcc`, `~/.codex-work` → `work`). A `label` in config
overrides, and Claude and Codex labels share one namespace, so colliding labels get a
numeric suffix (a codex root can't shadow a Claude account, or vice versa).

**Codex on WSL:** if you run Codex on the Windows side but ccusage under WSL, point a
`codex_roots` entry at the Windows directory so its usage shows up:

```json
{
  "codex_roots": [
    { "path": "/mnt/c/Users/<you>/.codex", "label": "codex-win" }
  ]
}
```

Windows-side roots under `/mnt/c` cross the WSL filesystem boundary, so the **first** scan
of a large Codex history is slower than a native root; after that the warm cache reads only
newly appended rollout bytes and the scan runs in the background without blocking the UI.

**Once you have more than one account (Claude or Codex):**

- **`a` cycles the account scope** — `all` → each isolatable account → `all`. The scope
  applies to *every* view (rolling windows, the heartbeat, the by-model table, and the
  date-range screen); selecting one account isolates it and excludes everything else.
  Claude accounts are always in the cycle; Codex accounts join it once you have **more than
  one** codex root (a single `~/.codex` stays lumped into `all`, as before). The active scope
  is shown at the top of the panel.
- **A By account block** appears under the model table in `all` scope: one row per account
  (Claude accounts plus a row per Codex account with in-window usage) with tokens, cost, and
  share-of-cost.
- **Subscription limits are fetched per Claude account** from each root's own credentials and
  the bars are labelled with the account (`PERSONAL 5-HOUR`, `RDQCC WEEKLY`); one account's
  fetch failure keeps its last-good values without blocking the others. Codex limits stay a
  single `CODEX` fetch (there is no per-root Codex limits RPC).
- **Settings → Accounts** lists every discovered root (label, provider, source, path) and
  lets you toggle each one on/off with **Enter**; the choice persists and the next scan
  reflects it.

A plain single `~/.claude` + `~/.codex` setup is unaffected: no scope line, no By account
block, and `a` does nothing — the panel is byte-for-byte what it was before.

## Pricing (editable)

Costs are the **API-equivalent dollar value** of your tokens (informational — you are on a
subscription). Rates live in `~/.config/cc-usage/pricing.json` (USD per 1M tokens) and are
yours to edit; a malformed file falls back to the bundled defaults. The cost model:

```
cost =  input_tokens              * input_rate
      + output_tokens             * output_rate
      + cache_read_input_tokens   * cache_read_rate
      + ephemeral_5m_input_tokens * input_rate * 1.25
      + ephemeral_1h_input_tokens * input_rate * 2.00
```

(If a record lacks the `ephemeral_*` sub-buckets, cache creation falls back to
`cache_creation_input_tokens * input_rate * 1.25`.) When a row omits `cache_read`, the
legacy 10% input-rate rule is used. Bundled defaults cover Claude plus published standard
API rates for GPT-5.4, GPT-5.4 mini, GPT-5.5, and the GPT-5.6 family. The cost engine also
applies the official >272K-input multipliers to models that publish them. Subscription-only
aliases, research previews without a token rate, local models, and future unknown ids
keep their token counts but are shown as **unpriced**; their unavailable cost is excluded
from totals, pricing coverage is reported, and they are flagged with a `*`.

## How it works

- **Claude transcripts** (`~/.claude/projects/**/*.jsonl`, read-only) are scanned
  recursively, including subagent/workflow usage. Streaming records are merged by
  `(requestId, message.id)`.
- **Codex / ChatGPT rollouts** (`~/.codex/sessions/**/*.jsonl` and
  `~/.codex/archived_sessions/*.jsonl`, read-only) contribute each response's
  `last_token_usage` and active model. Local rate-limit snapshots are fallback data only.
- **Live limits:** Claude is fetched from its OAuth usage endpoint; Codex is fetched via
  `account/rateLimits/read`. Normalized last-good results are cached without credentials.
- **Rolling windows** (last 1h / 5h / 24h / **7d** / all-time) are computed from record
  timestamps — pure epoch math, timezone-independent.
- **Heartbeat series** — the records in the chosen window are bucketed into ~48 equal time
  buckets; each bucket holds the summed cost or tokens for that slice (rate-per-bucket).
- **Incremental parsing:** each file's byte offset + size + mtime are remembered; later
  refreshes read only newly appended lines (no full re-scan per tick) and stay smooth across
  hundreds of files.
- **Immediate warm startup:** cached combined Claude + Codex aggregates are loaded and
  rendered before walking the transcript trees. File discovery, archive-move reconciliation,
  and appended-byte parsing continue in the background. Codex moving a rollout from active
  to archived storage preserves the warm cache instead of triggering a cold rebuild.
- **Persistent incremental cache:** `parse-cache.pkl` remembers every file offset. A pricing
  change, true transcript deletion, incompatible format, or corrupt cache safely falls back
  to a cold scan; results remain identical. The first-ever cold scan also runs off the UI
  thread. Discovery starts with an indeterminate status, followed by a byte-weighted
  progress bar with file and byte counts. Press `c` to cancel at a line boundary and
  `r` to resume without duplicating already parsed usage.
- **Memory-safe cold scans:** changed rollouts stream through a bounded read buffer rather
  than being copied into RAM whole, avoiding a history-sized allocation on large installs.
- **Adaptive terminal layout:** below 76 columns, rolling windows transpose vertically,
  model token columns collapse into a readable total, and scan progress wraps to two
  lines. No metrics are dropped.
- **Optional JSON acceleration:** if
  [`orjson`](https://github.com/ijl/orjson) is installed, ccusage uses it to speed up cold
  parsing; it is not required.

## Files ccusage owns

Derived data and settings live under `~/.config/cc-usage/`:

```
config.json                  your settings
pricing.json                 editable price table
provider-limits.json         normalized last-good Claude and Codex limits
parse-cache.pkl              warm-start parse cache (derived; safe to delete anytime)
backups/                     legacy statusline restore data, if an older version made it
```

Claude and Codex transcript directories remain read-only. The only provider-owned write
that may occur is Claude's own executable refreshing its expired OAuth credentials.

## Tests

```bash
uv run pytest -q
```

Covers the cost model, dedup/extraction against hand-verified fixtures, rolling-window
boundaries, heartbeat rendering, config persistence, keyboard-driven Textual flows,
Claude and Codex provider normalization, expired Claude credential refresh, credential
non-leakage, Codex rollout parsing, legacy statusline restoration, and incremental
warm-cache parsing.

## Scope & safety

- Live subscription limits require network access. Codex authentication stays behind its
  app-server broker. Claude's OAuth access token is read only in memory; persisted limit
  data contains percentages, labels, capture times, and reset times only.
- `~/.claude` and `~/.codex` transcripts are read-only. ccusage never changes a
  statusline. The hidden restore command changes Claude settings only when explicitly
  invoked to remove an older ccusage integration.
- Provider failures retain the last good normalized snapshot and surface a warning.
  Subscription percentages are provider-reported current values, distinct from the
  locally calculated token and API-equivalent cost history.
