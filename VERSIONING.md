# Versioning policy

cc-usage follows [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

## Scheme

Versions are `MAJOR.MINOR.PATCH` (e.g. `2.0.0`). Git tags are the same string
prefixed with `v` (e.g. `v2.0.0`).

| Bump      | When                                                                 |
|-----------|----------------------------------------------------------------------|
| **MAJOR** | Incompatible or visibly breaking changes (CLI flags removed/renamed, output contract or config format broken, a supported Python version dropped). |
| **MINOR** | Backwards-compatible new features (a new view, a new flag, a new export). |
| **PATCH** | Backwards-compatible bug fixes (no new features, no breakage).        |

### Pre-releases

Pre-release builds use a SemVer suffix on the next target version:

- `X.Y.Z-rc.N` — release candidate (e.g. `2.1.0-rc.1`).
- `X.Y.Z-beta.N` — earlier, less stable preview (e.g. `2.1.0-beta.1`).

A pre-release sorts **before** its final release (`2.1.0-rc.1` < `2.1.0`). Tag
pre-releases the same way: `v2.1.0-rc.1`.

## Single source of truth

The version is defined **once**, in [`cc_usage/__init__.py`](cc_usage/__init__.py):

```python
__version__ = "2.0.0"
```

`pyproject.toml` reads it dynamically — it is **not** a second source:

```toml
[project]
dynamic = ["version"]

[tool.setuptools.dynamic]
version = {attr = "cc_usage.__version__"}
```

Read it at runtime with `ccusage --version`, or in Python via
`cc_usage.__version__`. Never hard-code the version anywhere else.

## Release flow

Cutting a release is six steps. Steps 1–5 are local; pushing the tag (step 5)
fires the Release workflow, and only step 6 runs in CI.

1. **Bump the version.** Edit `cc_usage/__init__.py` and set `__version__` to the
   new `X.Y.Z`.
2. **Update the changelog.** Move the `Unreleased` entries in
   [`CHANGELOG.md`](CHANGELOG.md) into a new `## [X.Y.Z] - YYYY-MM-DD` section.
3. **Commit** the bump and changelog together:

   ```bash
   git commit -am "release: vX.Y.Z"
   ```

4. **Tag** the commit. The tag **must** equal `v` + `__version__`:

   ```bash
   git tag vX.Y.Z
   ```

5. **Push the tag:**

   ```bash
   git push origin vX.Y.Z
   ```

6. **CI cuts the release.** The `release.yml` workflow (triggered on `v*` tags):
   - **Guards** that the tag matches the source version — it fails the release if
     `vX.Y.Z` does not equal `v$(python -c 'import cc_usage; print(cc_usage.__version__)')`.
     This runs **before** anything is published, so a mismatched tag never ships.
   - **Builds** an `sdist` and a `wheel` and **attaches both to the GitHub
     Release**, so installing and self-updating never require PyPI.
   - **Publishes to PyPI** only when a `PYPI_API_TOKEN` secret is configured; on
     repos without the token this step is skipped (PyPI is never hard-required).

## How `--update` / `--check-update` consume releases (and the test channels)

The self-updater reads the project's **GitHub Releases**, not PyPI:

- `ccusage --check-update` queries the GitHub *latest release* API and compares
  its tag against `cc_usage.__version__` (a leading `v` is ignored in the
  comparison). It reports whether you are current and installs nothing.
- `ccusage --update` upgrades to the latest release tag via
  `pip install --upgrade git+https://github.com/ZhuoQiuMcgill/cc-usage.git@<tag>`.
  If no release is published yet, it falls back to `@main`. On a `uv tool`
  install (no pip in that environment), it detects that and runs
  `uv tool upgrade cc-usage` instead — see *Installer routing* below.

These are explicit user actions and are never reached from the panel/data path.
Because the updater keys off release **tags**, the tag/version guard in step 6 is
what keeps a published release installable: the tag always matches the version
users end up running.

### Test channels (`--update-pr` / `--update-prerelease` / `--update-stable`)

Beyond the stable release, the updater can install **unreleased / test** builds —
for trying work that isn't merged or published yet — and then return to the
official release:

- `ccusage --update-pr <N>` installs the head of **open PR #N** directly from the
  git ref GitHub exposes for public repos (`refs/pull/<N>/head`):
  `pip install --upgrade --force-reinstall git+...@refs/pull/<N>/head`. An unmerged
  PR is **not** a release, so this is the only way to reach it. It installs
  **unreviewed** code and prints a caution to that effect.
- `ccusage --update-prerelease` installs the newest GitHub release whose
  `prerelease` flag is set (a `-rc.N` / `-beta.N` tag, see *Pre-releases* above);
  if none is published it falls back to `@main`.
- `ccusage --check-prerelease` reports the installed version against the latest
  prerelease tag and installs nothing.
- `ccusage --update-stable` is the **return path**: it force-reinstalls the latest
  stable release tag (`/releases/latest`), restoring an official build.

All three install commands force a reinstall (pip's `--force-reinstall`, or
`uv tool install --force` on a uv tool install — see *Installer routing* below).
This is required because feature branches and test builds **do not bump
`__version__`** (the bump happens at release time, per the flow above), so a
test build can share the stable version string; without forcing, the installer
would treat the target as already-satisfied and not switch builds. The plain
`--update` path does **not** force-reinstall — it only moves between distinct
release tags.

These commands, like `--update` / `--check-update`, touch the network **only** as
explicit user actions, use the public-repo release/PR APIs and `git+https` (no
auth), and are never reached from the panel/data path. Because a machine on a
stable build doesn't yet have these flags, the very first test-build install is a
one-time manual pip or uv line (the bootstrap documented in the README);
afterward the `--update-*` commands are available.

### Installer routing (pip vs uv)

Every command above is written assuming a pip-bearing environment (pipx, or pip
in a venv), which is what most of the codebase and docs default to. A
`uv tool install` environment has no pip at all, so `cc_usage/update.py` detects
that at call time — pip unimportable in the running interpreter, `uv` present on
`PATH` — and swaps in the `uv` equivalent instead of failing:

| pip (pipx / venv)                                        | uv tool install                           |
|-----------------------------------------------------------|---------------------------------------------|
| `pip install --upgrade git+...@<tag>`                      | `uv tool upgrade cc-usage`                   |
| `pip install --upgrade --force-reinstall git+...@<ref>`    | `uv tool install --force git+...@<ref>`      |

The left column backs the plain `--update` path; the right backs every
force-reinstall command (`--update-pr`, `--update-prerelease`,
`--update-stable`). The detection and dispatch live in `_should_use_uv()` and
`_install()`; `_pip_install()` and `_uv_install()` are the two backends, kept
small and independently mockable so the test suite never runs pip, uv, or the
network for real.

### Windows: replacing ccusage's own running executable

Both backends above route their subprocess call through a shared `_run_install()`,
which exists to handle one more Windows-only wrinkle: running `ccusage --update*`
invokes ccusage's own launcher `.exe`, and while that process is alive Windows won't
let pip/uv overwrite its image (a Win32 sharing violation — `os error 32` / `WinError
32`, "the process cannot access the file because it is being used by another
process"). Unix has no such restriction, so this never happens there. The underlying
package install itself typically still succeeds; only the final entry-point refresh
fails.

`_run_install()` captures the install command's combined output (rather than
streaming it live) so `_is_windows_self_replace_error()` can check a failed run for
that exact OS-level phrase, gated on `sys.platform`. When it matches, `_run_install()`
prints the captured output as usual plus `_SELF_REPLACE_HINT`, telling the user the
package is likely already updated (verify with `ccusage --version`) and that closing
other running `ccusage` windows and re-running — or running the same command once via
`python -m cc_usage` instead of `ccusage` — refreshes the launcher too.
