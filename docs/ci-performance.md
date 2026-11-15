# CI image performance

The Phase 2 image gate still builds, inspects, and smoke-tests all eleven image
profiles whenever an image input changes. Its faster path comes from sharing
work, not from removing validation.

## Repository optimizations

`make images` creates one controller build record and submits the complete Bake
matrix in one BuildKit solve. Every target retains its profile-specific
`SW_IMAGE_PROFILE`, Python base, runtime packages, labels, embedded build record,
and final tag. The shared solve allows BuildKit to deduplicate common stages and
schedule independent profile tails concurrently.

The Dockerfiles keep volatile commit, timestamp, build-ID, and profile metadata
after dependency installation and engine acquisition. Worker targets declare
three stable cache families:

- `SW_WORKER_DEPENDENCY_SET` separates standard workers from Piper;
- `SW_ENGINE_PAYLOAD` separates DECTalk, VoiceText Paul, Speechify, and workers
  without downloaded engine payloads;
- `SW_RUNTIME_FAMILY` groups final images with the same operating-system
  packages.

uv and Cargo use BuildKit cache mounts. Lockfiles, source checksums, pinned image
digests, and the existing image inspection remain the correctness authorities;
cache contents are never accepted as provenance.

Both CI providers classify changed paths with `tools/ci/image_changes.py`.
Documentation-only changes run a whitespace/documentation gate; test and
quality-only changes run the Python checks without building images; runtime or
image inputs run the complete Python and image gates. Unknown paths fail closed
to the complete gate. Tags and manual runs always run the complete matrix.
Update the classifier whenever a new file becomes a runtime or image input.

The classifier uses these categories:

| Category | Examples | Gates |
| --- | --- | --- |
| `docs` | `docs/`, Markdown, `README.md`, `LICENSE` | diff check only |
| `tests` / `quality` | `tests/`, `quality/`, quality tooling | Python checks |
| `config` / `ci` | deployment config, workflow files | Python checks |
| `runtime` | `seasonalweather/` | Python checks and images |
| `image` | Dockerfiles, Bake, engine assets, image tooling | Python checks and images |
| `unknown` | newly unclassified paths | Python checks and images |

Forgejo already cancels superseded push and pull-request workflow runs by
default. GitHub CI declares an equivalent branch/ref concurrency group.

## Runner operations

Forgejo intentionally uses the persistent BuildKit cache owned by
`slate-builder`; registry cache export previously exceeded the proxy upload
limit. Do not routinely prune that builder. If it must be replaced, expect one
cold matrix build.

A versioned runner job image containing Docker CLI/Buildx, Python, uv, and the
basic build packages can remove most of the remaining setup time. That image is
runner administration, not a repository build input, and must be rolled out on
`slate-builder` separately before the workflow can stop installing its fallback
tooling.

## Release images

Release workflows still rebuild from the tagged source. Reusing CI candidates
requires a provider-neutral digest handoff plus a registry promotion contract;
local Docker tags are not portable between GitHub hosted jobs. Do not replace
the release build with mutable local tags. A future promotion change must pass
the inspected candidate digests between jobs and publish those exact manifests.

## Measuring results

Use the BuildKit plain-progress output to compare cold and warm runs. Record the
duration and cache-hit state for dependency installation, each engine payload,
application installation, final export, image inspection, and the three real
TTS smokes. Do not infer cache-hit rates from total workflow duration alone.
