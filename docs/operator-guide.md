# SeasonalWeather operator guide

This guide describes the supported Docker Compose deployment. It assumes the
operator has Docker Compose, a configuration file, and the three required
secret files. For a short first run, see the [root README](../README.md).

## Runtime layout

The application is distributed as images. A normal runtime host does not need
the Python source tree or a Rust toolchain.

```text
Compose controller       controller image
Compose workers           worker images, one per synthesis profile
Liquidsoap                liquidsoap image
Icecast                   optional icecast image or external service
Configuration             /etc/seasonalweather/config.yaml (read-only mount)
Secrets                   /run/secrets/* (read-only Compose secrets)
State/jobs/artifacts      /var/lib/seasonalweather/* (named volumes)
```

When images are built from this repository, the controller build compiles and
ships `/usr/local/bin/samegen` and `/usr/local/bin/samedec`. The old host path
`/opt/seasonalweather/samedec` belongs to the retired bare-metal installer and
is not part of the container deployment.

## Required configuration

Copy `config/config.yaml` and edit the live copy. At minimum, review:

| Section | Purpose |
| --- | --- |
| `station` | station name, service description, timezone, and presentation |
| `service_area` | transmitter SAME/FIPS and UGC targeting |
| `observations` | ASOS/AWOS station identifiers |
| `stream` | Icecast endpoint and mount |
| `nwws`, `cap`, `ern` | source enablement and upstream settings |
| `policy`, `same` | tone-out and SAME behavior |
| `tts` | backend and worker/profile selection |
| `paths` | container state, job, artifact, and temporary paths |
| `api.auth` | static, exchange, or hybrid API authentication |

The repository configuration is an example. The mounted live file is the
source of runtime behavior. Validate a candidate before applying it:

```bash
seasonalweather config lint --config ./seasonalweather.config.yaml
```

Inside a container, use the application CLI or the authenticated configuration
validation API described in
[`configuration-validation.md`](configuration-validation.md).

## Secrets

Compose expects these files under `SEASONALWEATHER_SECRET_DIR` (default
`./secrets`):

```text
ICECAST_SOURCE_PASSWORD
SEASONAL_API_TOKEN
SEASONAL_WORKER_TOKEN
```

Keep the directory private and each secret file mode `0400`. Do not place
credentials in the YAML file, image build arguments, Git, or Compose command
lines.

## Workers and TTS

The base stack starts the controller and routine worker. Optional worker
profiles are enabled explicitly:

```bash
docker compose --profile piper up -d
docker compose --profile espeak up -d
docker compose --profile festival up -d
docker compose --profile dectalk up -d
docker compose --profile legacy-tts up -d
docker compose --profile voicetext-paul up -d
docker compose --profile spfy up -d
```

The controller does not execute worker-only local TTS handlers. Workers stage
results; the controller validates, fences, promotes, and publishes accepted
artifacts. A missing or unhealthy required worker should be treated as a
readiness/degraded-service condition, not worked around by copying worker
packages into the controller image.

Native SAME tools are available in locally built controller images:

```bash
docker compose exec controller samegen --help
docker compose exec controller samedec --version
```

Set `same.native_encoder.enabled: true` to opt into `samegen`. Keep
`same.native_encoder.fallback_to_python: true` unless the deployment
deliberately requires native encoding. Leave `ern.decoder_backend: auto` to
prefer `samedec` and fall back to the Python decoder when it is unavailable.

## Health, logs, and diagnostics

```bash
docker compose ps
docker compose logs --tail=100 controller
docker compose logs --tail=100 routine-worker
curl http://127.0.0.1:9080/healthz
curl http://127.0.0.1:9080/readyz
```

The controller API is not published to the host by the base Compose file.
Expose it only through an intentional loopback or secured reverse-proxy
mapping. The OpenAPI document and Swagger UI are available at `/openapi.json`
and `/docs` when the API is reachable.

Runtime diagnostic definitions are immutable packaged content; runtime
occurrences are bounded operational state. See the
[diagnostic catalog](diagnostic-catalog.md) and
[runtime diagnostics](runtime-diagnostics.md).

## Persistence and upgrades

Named volumes hold SQLite operational state, jobs, artifacts, logs, and
profile-specific state. Back them up before upgrades and do not remove them
with `docker compose down -v`.

Use `docker compose config --quiet` before starting a changed configuration.
For a production cutover, use the reviewed procedure in
[`p3-08-production-migration.md`](p3-08-production-migration.md), including
image digest capture, readiness checks, rollback references, and evidence.

## Legacy bare-metal installations

The old `scripts/00-bootstrap.sh`, systemd units, `/opt/seasonalweather/app`
layout, and host-local `samedec` installer are retained for migration and
historical reference. They are not the recommended installation path for new
deployments. Do not mix a legacy host installation with the Compose volumes or
assume that editing the repository template changes a running container.

The legacy procedure is documented in [`INSTALLER.md`](INSTALLER.md).
