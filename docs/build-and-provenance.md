# Build interface and provenance

P2-01 establishes one repository build interface for local development and CI.
The stable entrypoints are:

```text
make check
make image
make images
make compose-check
make release
```

`make check` runs compilation, the repository quality gates, and the complete
test suite. CI calls the same target. `make image` and `make images` invoke the
declarative `docker-bake.hcl` matrix through the thin
`tools.build_interface` wrapper. Each image target generates its own
profile-specific build record before the target is built. The controller and
worker Dockerfiles remain owned by P2-02 and P2-03; until those packets add
them, image execution fails closed at Docker's missing-definition boundary.
`compose-check` remains a bounded placeholder until the Phase 3 Compose packet
introduces a topology.

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
