# Build interface and provenance

P2-01 establishes one repository build interface for local development and CI.
The stable entrypoints are:

```text
make check
make phase2-gate
make phase3-gate
make image
make images
make compose-check
make release
make release-artifacts
make release-images
```

`make check` runs compilation, the repository quality gates, and the complete
test suite. `make phase2-gate` adds the complete image matrix and built-image
contract inspection used by CI. `make image` and `make images` invoke the
declarative `docker-bake.hcl` matrix through the thin
`tools.build_interface` wrapper. Each image target generates its own
profile-specific build record before the target is built. P2-02 owns the
non-root controller Dockerfile and its controller-only dependency group;
P2-03 owns `Dockerfile.worker`, the worker dependency groups, the worker
entrypoint, and the routine-worker, Piper, legacy-TTS, VoiceText Paul, spfy,
maintenance, and development profiles. The controller Dockerfile rejects non-controller
profiles, while worker definitions reject controller-only profiles and
dependencies.
`compose-check` validates the checked-in `compose.yaml` when Docker Compose is
available. The topology is defined by P3-01; it does not build images or
start services. Compose deployment still requires operator-provided
configuration, mode-0400 secret files, and compatible controller/worker image
references.
`phase3-gate` runs the repository check interface and then validates the
checked-in Compose graph. It is the source-side Phase 3 gate; staging reboot,
stream, rollback, and production migration evidence remain operator-owned
acceptance items documented in [`p3-08-production-migration.md`](p3-08-production-migration.md).

## Build record

`make build-info` writes the generated record to the ignored `build/` tree.
Image builds embed the same record at:

```text
/usr/share/seasonalweather/build-info.json
```

The record includes the software version, Git commit and description,
dirty-tree state, source timestamp, effective `SOURCE_DATE_EPOCH`, deterministic
build ID, image profile, target platform, Python version, SWWP and job/result
schema versions, validation protocol versions, configuration-schema range, and
diagnostic/capability versions. A build ID supplied through `BUILD_ID` is
accepted as an explicit controlled input; otherwise it is derived from the
record's other controlled fields.

The unqualified Bake matrix defaults to the post-modernization release target
`0.18.0`; an explicit generated build-info record supplies the authoritative
version for controlled builds.

## Native SAME tool build

The controller target has a dedicated Rust BuildKit stage. It compiles the
repository-owned `tools/samegen` crate and installs the pinned `samedec` crate
release into the controller image as `/usr/local/bin/samegen` and
`/usr/local/bin/samedec`. The default inputs are exposed in
`docker-bake.hcl` as `RUST_IMAGE` and `SAMEDEC_VERSION`; the latter is kept in
step with the legacy installer default for migration compatibility.

Worker images intentionally do not receive these binaries. Their image
boundary removes controller-owned SAME, broadcast, persistence, and source
adapters, so native SAME encoding and ERN decoding remain controller
responsibilities. Python implementations remain available as configured
fallbacks.

Only these build-time metadata inputs are accepted by the build interface:

```text
SOURCE_DATE_EPOCH
BUILD_ID
BUILD_PROFILE
TARGET_PLATFORM
```

Arbitrary environment variables are not forwarded to Docker Buildx. Dirty
local source trees are marked explicitly. Release provenance requires an
explicit `SOURCE_DATE_EPOCH` and a clean tree.

`make release-artifacts` builds the PEP 517 source distribution and wheel into
`dist/release/` and writes a `SHA256SUMS` manifest. The Forgejo and GitHub
Release workflows invoke this target only after their CI, security, and
SemVer guardrail workflows pass for the tagged commit.

`make release-images` builds each target in the declared Bake matrix with its
profile-specific build record and pushes an explicit release reference. It
requires `IMAGE_REPOSITORY_BASE` and `IMAGE_TAG`, and writes the resulting
profile-to-image mapping to `IMAGE_REFERENCES_FILE` (by default
`dist/release/IMAGE-REFERENCES.txt`). Release workflows authenticate to their
own container registry before invoking this target. The GitHub workflow uses
GHCR and the Forgejo workflow uses the instance container registry; neither
workflow uses mutable `latest` references.

The Forgejo release workflow sets `SW_IMAGE_PUSH_MODE=engine`. Each release
image is first loaded into the runner-provided Docker image store with
BuildKit, then pushed by the Docker Engine client. This preserves the same
Forgejo registry hostname while allowing the Engine's registry client to use
streaming layer uploads when the layer is large. The default `buildkit` mode
remains available for local use and GitHub, which continues to use BuildKit's
direct registry exporter. The engine mode is intended to avoid oversized
monolithic registry requests; the actual request framing remains dependent on
the Docker Engine and registry client versions and should be confirmed from
registry/proxy logs.

CI and release workflows use best-effort caches. UV downloads are cached under
`~/.cache/uv` using the lockfile and project metadata as the key; the cache is
kept outside the repository so static analysis does not scan dependency files.
The type-check configuration also excludes generated `.cache` content as a
defense in depth measure. Docker BuildKit layers are cached independently for
each image profile. GitHub uses the Actions cache backend. Forgejo intentionally
does not export BuildKit cache layers through the instance registry: the
deployment's proxy rejects some large cache blobs with HTTP 413, and a failed
export wastes build time even when it is configured as best effort. Forgejo
therefore relies on the local cache of its dedicated runner-provided Docker
daemon when that daemon persists between jobs. Replacing or cleaning that
daemon only causes a slower uncached build; it does not affect build
correctness. A missing or unavailable cache does not replace the normal
dependency installation or image build. The cache is an acceleration mechanism
only; quality, test, image inspection, and release publication gates still run.

Forgejo's automatic workflow token is sufficient for creating the Forgejo
release but is not package-scoped. The Forgejo workflow or organization
Actions secrets must define `PACKAGE_REGISTRY_USER` and
`PACKAGE_REGISTRY_TOKEN`, where the latter is a user token with `write:package`
access and the user can write to the package owner. Release publication
requires these credentials. The ordinary CI image job does not require
package-registry credentials because it uses only the runner-local BuildKit
cache.

The runtime loads the embedded record when present and uses a bounded source
fallback for unbuilt checkouts. The same identity feeds `seasonalweather
version [--json]`, authenticated `GET /v1/version`, controller startup logs,
configuration validator stamps, and default SWWP registration fields.

## Runtime compatibility admission

Before normal controller work begins, the runtime compares the immutable build
record with the supported release contract. It requires the current software
release family, an overlapping SWWP, validation, job, configuration,
diagnostic, and capability schema version, and a role-appropriate image
profile. Controllers accept `controller` and the unbuilt `source` profile;
workers additionally require the embedded profile to match the selected worker
profile, with `source` retained as the development fallback.

A well-formed record that fails this comparison is rejected with
`SWBUILD2001` and startup does not proceed. A record that cannot be parsed or
validated remains `SWBUILD1001`. The check is implemented in
`seasonalweather.build_metadata.compatibility` and is shared by controller
and worker entrypoints.
