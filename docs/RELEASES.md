# SeasonalWeather release process

SeasonalWeather uses SemVer release tags as the stable update boundary.

## Policy

- `main` may move without a version bump.
- Documentation-only commits do not need a version bump.
- A release exists only when an annotated `vX.Y.Z` tag points to the checked-out release commit; package version metadata is derived from that Git tag.
- Stable update checkers should consume release tags, not arbitrary `main` commits.
- Release tags should be treated as immutable after they are published. Retarget a release tag only for an immediate correction before anyone has consumed it.

## Version source

The canonical stable version is the annotated Git tag. Development versions are
derived from the nearest tag by `setuptools-scm`.

Other release metadata should be generated from or checked against the package
version produced by the Git metadata.

## Cutting a release

Use the release helper instead of manually editing the version and creating a tag:

```bash
tools/release.sh 0.11.0
git push origin main v0.11.0
```

The helper validates the requested SemVer, ensures the release is newer than the latest existing `v*` release tag, runs compile/tests, creates a release commit, and creates an annotated tag.

For an intentional maintenance release from a non-`main` branch:

```bash
ALLOW_NON_MAIN=1 tools/release.sh 0.10.1
```

Avoid skipping tests for normal releases. `SKIP_TESTS=1` exists only for constrained emergency/operator situations.

## CI guardrails

The canonical Forgejo workflows, with equivalent GitHub workflow parity,
validate that:

  - the Git-derived working-tree version is valid SemVer, including development versions.
- Release tags are named `vX.Y.Z`.
- Release tags are annotated.
  - the annotated tag names the checked-out release commit.
- The tag is newer than the previous SemVer release tag.

This prevents stale-version releases and keeps untagged development builds distinguishable from stable tags.
