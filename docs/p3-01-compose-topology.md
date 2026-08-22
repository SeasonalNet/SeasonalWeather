# P3-01 Compose service topology

`compose.yaml` is the standard Phase 3 topology for the controller-side
SeasonalWeather deployment. It keeps the controller, routine worker, and
Liquidsoap in the default graph. The routine worker is mandatory; the
controller does not have an embedded-executor fallback.

## Services and profiles

| Service | Default | Authority | Image default |
| --- | --- | --- | --- |
| `controller` | enabled | API, durable state/jobs, scheduling, SWWP endpoint, promotion | `seasonalweather:standard` |
| `routine-worker` | enabled | routine SWWP handler execution | `seasonalweather-worker:standard` |
| `liquidsoap` | enabled | playout queues and stream output | `savonet/liquidsoap:v2.3.3` |
| `maintenance-worker` | `maintenance` profile | maintenance SWWP work only | `seasonalweather-worker:maintenance` |
| `icecast` | `icecast` profile | optional stream server | `ghcr.io/libretime/icecast:2.5.0` |

The `maintenance` profile is not part of the routine worker's capability
surface. Enabling it adds a separate maintenance-queue worker; it does not
grant routine or TTS work. The `icecast` profile is an optional colocated
stream server. Without that profile, `SEASONALWEATHER_ICECAST_HOST` must point
to an external Icecast service.

`seasonal-ttsd`, OpenAI-compatible TTS providers, and PostgreSQL are not
Compose services in P3-01. They remain external dependencies until their
dedicated packets define their deployment profiles and failure tests.

## Connectivity and readiness

Workers initiate authenticated outbound SWWP/1 connections to:

```text
ws://controller:9080/v1/workers/connect
```

The worker bearer token is mounted only at `/run/secrets/SEASONAL_WORKER_TOKEN`,
owned by the non-root worker UID/GID `10001` with mode `0400`.
The controller readiness check is intentionally not used as the worker's
startup dependency: readiness requires a qualified worker when durable jobs
are required, so making the worker wait for controller readiness would create
a startup cycle. Workers wait for the controller process to start and retry
their outbound connection through the SWWP transport.

The controller uses the internal `liquidsoap` service name for telnet control.
Liquidsoap uses `SEASONALWEATHER_ICECAST_HOST` and
`SEASONALWEATHER_ICECAST_PORT`; its script reads the source password from the
mounted secret file. The Compose default uses the `icecast` DNS name so the
optional profile can be enabled without changing the Liquidsoap service.

## Configuration and secrets

The repository example configuration is mounted read-only at
`/etc/seasonalweather/config.yaml`. Create a local, untracked `secrets/`
directory containing mode-0400 files named:

```text
ICECAST_SOURCE_PASSWORD
SEASONAL_API_TOKEN
SEASONAL_WORKER_TOKEN
```

`SEASONALWEATHER_SECRET_DIR` may point to another directory. The Compose
definition never stores secret values. Controller and Liquidsoap receive only
the secret files they need; workers receive only `SEASONAL_WORKER_TOKEN`.

For the optional Icecast profile, provide an external rendered Icecast XML
file through `SEASONALWEATHER_ICECAST_CONFIG_FILE`. Do not commit that file
when it contains credentials. The default path is `./config/icecast.xml` and
is expected to be supplied by the operator before enabling the profile.

## Image compatibility

The SeasonalWeather controller and worker image variables should identify
images built from the same release and schema/protocol family. The default
local tags match the P2 image matrix. Controlled deployments should override
the image variables with immutable release tags or digests as a unit:

```text
SEASONALWEATHER_CONTROLLER_IMAGE
SEASONALWEATHER_ROUTINE_WORKER_IMAGE
SEASONALWEATHER_MAINTENANCE_WORKER_IMAGE
```

P3-01 does not build images or alter the Bake matrix. It validates the Compose
topology; image construction and built-image inspection remain P2-09
responsibilities.

The controller and worker entrypoints are declared explicitly in Compose and
their `command` values contain only service arguments. This preserves the
Dockerfile entrypoint contract when a release image is selected. The
controller and workers are also explicitly run as UID/GID `10001:10001`,
matching the P2-05 non-root image contract.

## Authority boundary

The named state, jobs, artifact, and log volumes are the minimum runtime
mounts needed by the service graph. P3-01 does not define artifact promotion,
worker staging permissions, active-path protection, or recreation semantics.
Those authority-separated volume and artifact-flow rules belong to P3-02.

Likewise, P3-01 does not claim local-only TTS operation, remote-provider
integration, production deployment, restart, migration, or soak validation.
