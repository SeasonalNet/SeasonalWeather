# SeasonalWeather

SeasonalWeather is an unofficial, IP-based weather and alert radio service suite
inspired by NOAA Weather Radio workflows.

 It ingests NWS and related alert
sources, generates routine and interrupting audio, and publishes the result
through Liquidsoap and Icecast.

It is hobbyist-developed, and under [SeasonalNet](https://www.seasonalnet.org).

It is not a NOAA, NWS, FEMA, EAS, or NWR service and should not be used as a
life-safety alert source. For offcial alerts, you should use your regions resources, such as NOAA Weather Radio, [weather.gov](https://weather.gov) and local authorities.

## Overview

SeasonalWeather can combine:

- NWWS-OI, NWS API, CAP/IPAWS, and ERN/GWES inputs;
- cross-source deduplication and SAME/FIPS service-area filtering;
- routine forecast, observation, and outlook cycle audio;
- full SAME/tone/TTS alert cut-ins and lower-severity voice-only inserts;
- controller-owned audio validation, worker synthesis, and publication; and
- HTTP status/control surfaces, a handled-alerts feed, diagnostics, and
  structured operational logs.

The supported deployment model is Docker Compose.

- The controller owns alert state, configuration, scheduling, validation, artifact promotion, and
  publication.

- Workers run isolated synthesis profiles and additional tasks, they connect to the
  controller over SWWP/1.
  
- Liquidsoap and, optionally, Icecast run as separate
  Compose services.

## Scope

SeasonalWeather is capable of a lot, it has the ability to generate valid SAME tones, along with the 1050Hz Warning Alarm Tone for alerts. In addition, it is capable of producing text structured similarly to NOAA Weather Radio broadcasts.

Those are powerful capabilities and should be used responsibly.

Below is a list of cases SeasonalWeather **should** and **should not** generally be used for:

 SeasonalWeather **should** be used for:

- Non-critical deployments
- Hobbyist/enthusiast usage
- Research
- IP radio deployments (e.g. Icecast 2, etc.)

SeasonalWeather should **not** be used for:

- Replacing other sources of weather and alerting information
- Commercial usage where reliability is critical
- Over the air broadcasts (AM/FM, VHF, UHF, TV, etc.)
- Generating false alerts, messages, or tones which could be confused with an offical source

As always, with any software, you are responsible for your actions and should abide by your local and federal governments ordiances and laws.

SeasonalWeather is licensed under the GNU AGPLv3 license, see [LICENSE](./LICENSE) for details.

## Deployment model

Use published controller/worker images when available. The runtime host then
needs Docker Engine or a compatible Compose implementation, persistent volume
storage, a configuration file, and secret files; it does not need a
SeasonalWeather source checkout.

Build images locally only on a build machine:

```bash
make image                         # controller image
docker buildx bake routine-worker  # one worker profile
make images                        # complete image matrix
```

The controller image builds and includes both native SAME tools:

- `samegen`, compiled from `tools/samegen` in this repository;
- `samedec`, compiled from the pinned crate release declared by the Bake
  configuration.

The Python encoder and decoder remain supported fallback implementations.
Native encoding is opt-in through `same.native_encoder.enabled`; ERN decoding
uses `ern.decoder_backend: auto` by default and selects `samedec` when it is
available.

The old bare-metal/systemd bootstrap and host-local `samedec` installation are
legacy migration material. They are retained in the repository for reference,
but are not the deployment path for new installations.

## Quick start

### 1. Prepare a deployment directory

This checkout is needed only when building images locally or using the
repository-provided Compose files. A host using registry images can copy the
Compose/configuration assets without installing the Python application.

```bash
git clone https://git.seasonalnet.org/SeasonalNet/SeasonalWeather
cd SeasonalWeather
mkdir -m 700 secrets
cp config/config.yaml seasonalweather.config.yaml
```

Edit `seasonalweather.config.yaml` for station identity, timezone,
service-area FIPS/UGC settings, enabled sources, TTS policy, and API mode.
Create these files with mode `0400` and fill them with deployment secrets:

```text
secrets/ICECAST_SOURCE_PASSWORD
secrets/SEASONAL_API_TOKEN
secrets/SEASONAL_WORKER_TOKEN
```

The Compose defaults mount persistent state, jobs, artifacts, staging, logs,
and optional VoiceText Paul state into named volumes. Set
`SEASONALWEATHER_CONFIG_FILE` and `SEASONALWEATHER_SECRET_DIR` when those files
live elsewhere.

### 2. Select images

For local images, build the controller and routine worker:

```bash
docker buildx bake controller routine-worker
```

For registry images, set the image variables before starting Compose:

```bash
export SEASONALWEATHER_CONTROLLER_IMAGE=registry.example/seasonalweather:0.18.0
export SEASONALWEATHER_ROUTINE_WORKER_IMAGE=registry.example/seasonalweather-worker:0.18.0
```

Use immutable image digests for production rollback and record the selected
image identity before cutover.

### 3. Validate and start the stack

```bash
docker compose config --quiet
docker compose --profile icecast up -d
docker compose ps
```

The `icecast` profile publishes Icecast on port `8000`. Without that profile,
an external Icecast service may be selected through configuration and Compose
environment overrides.

Check startup and readiness:

```bash
docker compose logs --tail=100 controller
docker compose logs --tail=100 routine-worker
curl http://127.0.0.1:9080/healthz
curl http://127.0.0.1:9080/readyz
```

The default controller service is not published to the host. Add a deliberate
loopback-only port mapping when local API access is required, or use the
staging overlay documented in [`docs/p3-07-staging.md`](docs/p3-07-staging.md).

Typical Icecast mounts are:

```text
http://HOST:8000/seasonalweather.ogg
http://HOST:8000/seasonalweather.mp3
```

### 4. Stop, upgrade, and roll back

```bash
docker compose stop
docker compose up -d
docker compose logs -f controller
```

Do not use `docker compose down -v` for routine maintenance; named volumes
contain operational state and audio artifacts. The production migration and
rollback procedure is in
[`docs/p3-08-production-migration.md`](docs/p3-08-production-migration.md).

## Configuration and operations

The live configuration is mounted at `/etc/seasonalweather/config.yaml` inside
the controller. Persistent application paths are inside the container under
`/var/lib/seasonalweather`; Compose supplies the corresponding named volumes.
Secrets are mounted as read-only files under `/run/secrets`.

Start with the [operator guide](docs/operator-guide.md), which covers the
configuration sections, worker/TTS profiles, API authentication, logs,
diagnostics, native SAME tools, and common recovery commands.

## Documentation

[`docs/index.md`](docs/index.md) is the documentation map. Important starting
points include:

- [Operator guide](docs/operator-guide.md)
- [Build and image provenance](docs/build-and-provenance.md)
- [Configuration validation](docs/configuration-validation.md)
- [Compose authority and volumes](docs/p3-02-authority-separated-volumes.md)
- [Local TTS workers](docs/p3-03-local-only-tts.md)
- [Staging operation](docs/p3-07-staging.md)
- [Production migration and Phase 3 gate](docs/p3-08-production-migration.md)
- [API command and job contracts](docs/command-job-contracts.md)
- [Worker runtime and SWWP](docs/worker-runtime.md)
- [Diagnostics](docs/runtime-diagnostics.md)

The systemd bootstrap, installer profiles, and host-local decoder procedure
are retained as [legacy installer documentation](docs/INSTALLER.md) for
migration work only.

## Development

Development standards, architecture ownership, and required quality checks
are in [`CONTRIBUTING.md`](CONTRIBUTING.md) and
[`docs/quality-and-ci.md`](docs/quality-and-ci.md).

Run the repository checks with:

```bash
make check
make phase3-gate
```
