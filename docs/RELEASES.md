# SeasonalWeather release process

SeasonalWeather uses SemVer release tags as the stable update boundary.

## Policy

- `main` may move without a version bump.
- Documentation-only commits do not need a version bump.
- A release exists only when an annotated `vX.Y.Z` tag points to code whose `seasonalweather.__version__` is exactly `X.Y.Z`.
- Stable update checkers should consume release tags, not arbitrary `main` commits.
- Release tags should be treated as immutable after they are published. Retarget a release tag only for an immediate correction before anyone has consumed it.

## Version source

The canonical version is stored in:

```python
seasonalweather/__init__.py
```

Other release metadata should be generated from or checked against that value.

## Cutting a release

Use the release helper instead of manually editing the version and creating a tag:

```bash
tools/release.sh 0.11.0
git push origin main v0.11.0
```

The helper validates the requested SemVer, ensures the release is newer than the latest existing `v*` release tag, updates `seasonalweather/__init__.py`, runs compile/tests, commits the version bump, and creates an annotated tag.

For an intentional maintenance release from a non-`main` branch:

```bash
ALLOW_NON_MAIN=1 tools/release.sh 0.10.1
```

Avoid skipping tests for normal releases. `SKIP_TESTS=1` exists only for constrained emergency/operator situations.

## CI guardrails

The canonical Forgejo workflows, with equivalent GitHub workflow parity,
validate that:

- `seasonalweather.__version__` is valid SemVer.
- Release tags are named `vX.Y.Z`.
- Release tags are annotated.
- The tag name matches the checked-out code version.
- The tag is newer than the previous SemVer release tag.

This prevents stale-version releases, including the bad state where `v0.10.0` points at code still advertising `0.9.0`.
