# Build interface and provenance

P2-01 establishes one repository build interface for local development and CI.
The stable entrypoints are:

```text
make check
make phase2-gate
make image
make images
make compose-check
make release
```

`make check` runs compilation, the repository quality gates, and the complete
test suite. `make phase2-gate` adds the complete image matrix and built-image
contract inspection used by CI. `make image` and `make images` invoke the
declarative `docker-bake.hcl` matrix through the thin
`tools.build_interface` wrapper. Each image target generates its own
profile-specific build record before the target is built. P2-02 owns the
non-root controller Dockerfile and its controller-only dependency lock;
P2-03 owns `Dockerfile.worker`, the worker dependency locks, the worker
entrypoint, and the routine-worker, Piper, legacy-TTS, maintenance, and
development profiles. The controller Dockerfile rejects non-controller
profiles, while worker definitions reject controller-only profiles and
dependencies.
`compose-check` validates the checked-in `compose.yaml` when Docker Compose is
available. The topology is defined by P3-01; it does not build images or
start services. Compose deployment still requires operator-provided
configuration, mode-0400 secret files, and compatible controller/worker image
references.

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
