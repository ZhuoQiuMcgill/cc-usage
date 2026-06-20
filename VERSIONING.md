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

## How `--update` / `--check-update` consume releases

The self-updater reads the project's **GitHub Releases**, not PyPI:

- `ccusage --check-update` queries the GitHub *latest release* API and compares
  its tag against `cc_usage.__version__` (a leading `v` is ignored in the
  comparison). It reports whether you are current and installs nothing.
- `ccusage --update` upgrades to the latest release tag via
  `pip install --upgrade git+https://github.com/ZhuoQiuMcgill/cc-usage.git@<tag>`.
  If no release is published yet, it falls back to `@main`.

These are the **only** commands that touch the network; they are explicit user
actions and are never reached from the panel/data path. Because the updater keys
off release **tags**, the tag/version guard in step 6 is what keeps a published
release installable: the tag always matches the version users end up running.
