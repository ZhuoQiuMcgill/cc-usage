# ccusage

A keyboard-first, interactive terminal app for your **Claude Code** usage across **all**
sessions — per-model token counts and **API-equivalent dollar cost** parsed from local
transcripts, a compact usage **heartbeat**, rolling spend windows, plus your official
**5-hour and 7-day subscription limits**. So you never have to run `/usage` again.

**One command launches it; you drive everything — viewing, switching views, and all
configuration — with arrow keys + Enter. There are no flags to memorize.**

```
CC Usage
─────────────────────────────────────────────────────────────────────

5-HOUR   ▓▓░░░░░░░░░░░░   1%  resets in 3h14m
WEEKLY   ▓▓░░░░░░░░░░░░  13%  resets in 5d03h
─────────────────────────────────────────────────────────────────────

Spend      1h      5h      24h       7d   all-time
tokens   9.2M   51.0M   128.0M   402.0M       3.7B
cost    $8.40  $52.10  $131.50  $455.00  $4,120.00
─────────────────────────────────────────────────────────────────────

heartbeat cost · 24h   (left/right = window · up/down = metric)
$12.40 ⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⡇⠀⠀⠀⠀⠀⢠⠀⠀⠀
       ⠀⠀⠀⠀⠀⠀⠀⠀⠀⡆⢸⠀⠀⠀⣿⡆⢸⡇⠀⠀⣸⠀⠀⠀
 $6.20 ⠀⠀⠀⠀⠀⠀⠀⠀⠀⡇⢸⠀⠀⠀⣿⡇⣿⡇⠀⠀⣿⢰⡇⠀
 $4.10 ⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣸⠀⠀⠀⣿⡇⣿⡇⠀⠀⣿⢸⡇⢸
     0 ⣀⣀⣀⣀⣀⣀⣀⣀⣠⣿⣿⣀⣀⣀⣿⣷⣿⣿⣠⣇⣿⣿⣇⣸
       -24h  -18h  -12h  -6h  now
       peak $12.40/bucket · 14:30 (3h ago)
─────────────────────────────────────────────────────────────────────

By model · all-time
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

Requires **Python 3.10+** and `jq` (used by the statusline wrapper; you almost certainly
already have it). Any installer below puts the `ccusage` command on your `PATH` — **uv is
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
| **s** or **Enter** | Open **Settings** (refresh interval, default table window, show-cost, theme, and the statusline 5h/7d capture install/restore) |
| **↑ / ↓** | Move the selection inside Settings and its pickers; **Enter** confirms |
| **Esc** | Back out of Settings / a picker |
| **q** or **Ctrl-C** | Quit cleanly — the terminal is restored |

Data keeps refreshing on the configured interval while the UI stays responsive; the reset
timers and the heartbeat update live.

### The only flags (none required)

| Command | What it does |
|---|---|
| `ccusage` | Launch the interactive TUI (default). |
| `ccusage --once` | Print a single static frame and exit (handy for scripts / a statusline). |
| `ccusage --check-update` | Report your version vs the latest GitHub release (installs nothing). |
| `ccusage --update` | Upgrade to the latest release via pip. |
| `ccusage --update-pr <N>` | Install the head of open PR #N for testing (force-reinstall; **unreviewed code**). |
| `ccusage --update-prerelease` | Install the latest prerelease build (or `@main`) for testing (force-reinstall). |
| `ccusage --update-stable` | Return to the latest official release (force-reinstall). |
| `ccusage --check-prerelease` | Report your version vs the latest prerelease tag (installs nothing). |
| `ccusage --version` / `--help` | Version / usage. |

(`--install-statusline` / `--restore-statusline` still exist as **hidden** scriptable
aliases, but the primary, documented path is in-app: **Settings → Statusline 5h/7d
capture**.)

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
- **Statusline 5h/7d capture** — Install wrapper / Restore original

Choices persist to `~/.config/cc-usage/config.json` and apply live.

## The 5h / 7d limits — how capture works (and how to undo it)

Claude Code passes a JSON document on **stdin** to your `statusLine` command on every
assistant turn; for Pro/Max accounts it includes a `rate_limits` object. ccusage reads
those numbers **only** from a small local cache — it never touches credentials and makes
**no network calls** on the panel/data path.

To populate that cache, install the wrapper from **Settings → Statusline 5h/7d capture →
Install wrapper** (all keyboard-driven). The wrapper:

1. **Backs up** your `settings.json` and `statusline-command.sh` first.
2. Repoints `settings.json` → `statusLine.command` at a small wrapper script that reads
   stdin once, writes `.rate_limits` (+ a capture timestamp) to
   `~/.config/cc-usage/ratelimits.json`, then **runs your original statusline with the same
   stdin and emits its output byte-for-byte unchanged**. Install *proves* the output is
   byte-identical before it repoints anything — your visible statusline does not change.

The limits then appear within a few seconds, on your next turn. Until the wrapper is
installed (or if you use an API key with no subscription limits) the panel shows
`5h / 7d: n/a — run a Claude Code turn to populate`.

**Undo at any time** — fully reversible, from **Settings → Statusline 5h/7d capture →
Restore original**. Restore reverts **both** `settings.json` and `statusline-command.sh`
to their originals (verified by sha256), removes the wrapper, and clears the cached limits.
If you ever need to do it by hand:

```bash
cp -p ~/.config/cc-usage/backups/settings.json.orig         ~/.claude/settings.json
cp -p ~/.config/cc-usage/backups/statusline-command.sh.orig ~/.claude/statusline-command.sh
rm -f ~/.config/cc-usage/statusline-wrapper.sh ~/.config/cc-usage/ratelimits.json
```

## Pricing (editable)

Costs are the **API-equivalent dollar value** of your tokens (informational — you are on a
subscription). Rates live in `~/.config/cc-usage/pricing.json` (USD per 1M tokens) and are
yours to edit; a malformed file falls back to the bundled defaults. The cost model:

```
cost =  input_tokens              * input_rate
      + output_tokens             * output_rate
      + cache_read_input_tokens   * input_rate * 0.10
      + ephemeral_5m_input_tokens * input_rate * 1.25
      + ephemeral_1h_input_tokens * input_rate * 2.00
```

(If a record lacks the `ephemeral_*` sub-buckets, cache creation falls back to
`cache_creation_input_tokens * input_rate * 1.25`.) Unknown model ids contribute **$0**,
are flagged with a `*`, and never crash the tool. Model ids are matched tolerantly —
`[1m]` and date suffixes are stripped.

## How it works

- **Transcripts** (`~/.claude/projects/**/*.jsonl`, read-only) are scanned **recursively** —
  including subagent/workflow transcripts, which are real spend. Each assistant record with
  a usage object is **deduplicated by `(requestId, message.id)`** so retries/echoes count
  once.
- **Rolling windows** (last 1h / 5h / 24h / **7d** / all-time) are computed from record
  timestamps — pure epoch math, timezone-independent.
- **Heartbeat series** — the records in the chosen window are bucketed into ~48 equal time
  buckets; each bucket holds the summed cost or tokens for that slice (rate-per-bucket).
- **Incremental parsing:** each file's byte offset + size + mtime are remembered; later
  refreshes read only newly appended lines (no full re-scan per tick) and stay smooth across
  hundreds of files.

## Files ccusage owns

Everything under `~/.config/cc-usage/`:

```
config.json                 your settings
pricing.json                editable price table
ratelimits.json             last captured 5h/7d limits (only while the wrapper is installed)
statusline-wrapper.sh       the capture wrapper (only while installed)
backups/                    settings.json.orig, statusline-command.sh.orig, snapshots
```

## Tests

```bash
.venv/bin/python -m pytest -q
```

Covers the cost model, dedup/extraction against a hand-verified fixture (with a malformed
line, an unknown model, and a duplicate), rolling-window boundaries (incl. **7d**), the
**heartbeat series** (rate-per-bucket semantics, window scoping, edges, empty window, the
braille renderer), config persistence, the **interactive TUI flows** driven by Textual's
test harness (arrow/Enter navigation, heartbeat window/metric switching, opening Settings
and changing a setting, clean quit), and the **statusline install→restore** flow proven
byte-identical by sha256 — plus incremental parsing.

## Scope & safety

- **No credentials are ever read; no network calls on the panel/data path.** Limits come
  only from the local statusline capture. The only network access is the explicit,
  user-invoked update commands (`--update` / `--check-update` and the test-channel
  `--update-pr` / `--update-prerelease` / `--update-stable` / `--check-prerelease`); they
  use the public-repo release/PR APIs and `git+https`, which need no auth.
- `~/.claude` transcripts are treated as read-only. The only files ccusage modifies are the
  statusline settings/script, and only reversibly, with backups.
- Out of scope: the OAuth `/api/oauth/usage` endpoint, multi-user, remote, historical
  charts.
