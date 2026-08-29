# SeasonalWeather release process

SeasonalWeather uses SemVer release tags as the stable update boundary and PEP
440 for Python package, build, and runtime software versions.

## Policy

- `main` may move without a version bump.
- Documentation-only commits do not need a version bump.
- A release exists only when an annotated `vX.Y.Z` or `vX.Y.Z-<prerelease>` tag points to the checked-out release commit; package version metadata is derived from that Git tag.
- Stable update checkers should consume release tags, not arbitrary `main` commits.
- Release tags should be treated as immutable after they are published. Retarget a release tag only for an immediate correction before anyone has consumed it.

## Version source

The canonical stable VCS version is the annotated Git tag. Development package
versions are derived from the nearest tag by `setuptools-scm` and use PEP 440,
for example `0.18.0.dev12+gabc1234`. A SemVer prerelease tag such as
`v0.18.0-alpha.1` is exposed to Python as `0.18.0a1`.

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

  - the Git-derived working-tree version is valid PEP 440.
- Release tags are valid SemVer versions named `vX.Y.Z` or `vX.Y.Z-<prerelease>`.
- Release tags are annotated.
  - the annotated tag names the checked-out release commit.
- The tag is newer than the previous SemVer release tag.

This keeps the two dialects at their proper boundaries: SemVer is used for
VCS release identity and ordering, while PEP 440 is used wherever Python or a
runtime compatibility check consumes a software version.

## Automated release artifacts

Pushing an annotated `v*` tag starts the Release workflow after CI, Security,
and SemVer guardrails have all passed. It builds the source distribution and
wheel, plus `SHA256SUMS`, in `dist/release/`. It also builds and pushes all
declared controller and worker image profiles to the provider's container
registry. `IMAGE-REFERENCES.txt` records the exact image references included
by that release. The release is published only after the archives and images
are complete. No release is published for an untagged commit.

Container image tags are profile-qualified and immutable. For a release such
as `v0.18.0-alpha.2`, the controller and development images use the
`seasonalweather` repository, while worker profiles use
`seasonalweather-worker`; each image tag ends in its profile name. The Compose
deployment should use these exact release references rather than a mutable
`latest` tag.

The current provider locations are `ghcr.io/seasonalnet/seasonalweather` for
GitHub and `git.seasonalnet.org/seasonalnet/seasonalweather` for Forgejo.
Worker references use the corresponding `seasonalweather-worker` repository.
The release workflow writes the complete profile mapping to
`dist/release/IMAGE-REFERENCES.txt` so operators do not need to reconstruct
the names manually.
