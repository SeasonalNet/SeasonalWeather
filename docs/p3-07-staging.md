# P3-07 staging, failure injection, rollback, and soak

P3-07 provides an isolated staging operation for the accepted Phase 3 Compose
topology. It does not alter the default Compose project, the production
systemd services, or production configuration.

## Staging boundary

The staging overlay is `compose.staging.yaml`. It fixes the Compose project
name to `seasonalweather-staging`, enables the separate Icecast service, and
publishes only loopback ports by default:

| Surface | Default staging port | Production surface |
| --- | ---: | --- |
| Controller API | `127.0.0.1:19080` | not changed |
| Icecast | `127.0.0.1:18000` | not changed |

Compose names volumes by project. The fixed staging project therefore keeps
state, jobs, artifacts, staging files, logs, and optional voice volumes apart
from the default project. The overlay also routes Liquidsoap to the staging
Icecast service, creating an alternate output stream without changing the
production stream.

Staging requires two operator-supplied paths outside the repository:

```text
SEASONALWEATHER_STAGING_CONFIG_FILE=/absolute/path/to/staging-config.yaml
SEASONALWEATHER_STAGING_SECRET_DIR=/absolute/path/to/staging-secrets
```

The secret directory must contain `ICECAST_SOURCE_PASSWORD`,
`SEASONAL_API_TOKEN`, and `SEASONAL_WORKER_TOKEN`, each inaccessible to group
and other users. Secret contents are never committed or printed by the
staging interface. The staging configuration must disable production inputs
or point them at an explicitly approved alternate/test source. Do not point
it at `/etc/seasonalweather/config.yaml` or the repository's default config.

## Controlled operation

The interface always supplies both Compose files, the fixed project name, and
the `icecast` profile:

```bash
SEASONALWEATHER_STAGING_CONFIG_FILE=/absolute/path/to/staging-config.yaml \
SEASONALWEATHER_STAGING_SECRET_DIR=/absolute/path/to/staging-secrets \
  ./.venv/bin/python -m tools.staging_interface config
```

Available operations are `config`, `up`, `down`, `restart`, `recreate`, `ps`,
`logs`, `rollback`, and `soak`. Add `--profile piper`, `--profile legacy-tts`,
`--profile voicetext-paul`, `--profile spfy`, or `--profile maintenance` to
operate the corresponding optional worker; repeat `--profile` to select more
than one. `down` never removes named volumes. `recreate` therefore exercises
container replacement while preserving the durable state and audio volumes.

The staging operation matrix is:

1. Start the candidate image set and record build identities, service health,
   worker qualification, controller readiness, and the alternate stream.
2. Feed an approved alternate/test stream or bounded test-originated products;
   compare alert decisions, ordering, text, metadata, audio properties, and
   stream behavior with the corresponding production observations. Do not
   inject staging data into production.
3. Restart each service and repeat the health, worker, artifact, and stream
   checks. Recreate the project without volume removal and verify that state,
   jobs, controller-accepted audio, and Liquidsoap visibility survive.
4. Inject dependency outage, worker loss, capability degradation, TTS outage,
   diagnostic rejection, and stale-result scenarios. Record visible bounded
   diagnostics and verify that no stale result replaces current audio.
5. Test a host reboot on the staging host. Confirm all services recover and
   the same persistence and authority checks pass afterward.

Failure injection must use the existing controller, SWWP, capability, artifact,
diagnostic, and lifecycle boundaries. It must not add a worker-side publication
path, embedded controller executor, second scheduler, or second persistence
authority.

## Image rollback

Rollback is a coordinated image-set operation. Keep a rollback environment file
containing only the immutable prior image references, for example:

```text
SEASONALWEATHER_CONTROLLER_IMAGE=registry.example/seasonalweather@sha256:...
SEASONALWEATHER_ROUTINE_WORKER_IMAGE=registry.example/seasonalweather-worker@sha256:...
SEASONALWEATHER_LIQUIDSOAP_IMAGE=savonet/liquidsoap@sha256:...
SEASONALWEATHER_ICECAST_IMAGE=ghcr.io/libretime/icecast@sha256:...
```

Run `rollback --env-file /absolute/path/to/prior-images.env`; the interface
rejects non-image keys and mutable image tags, then recreates the staging
services without deleting volumes. Record the image identities before and
after rollback, then verify that the current accepted audio remains current
and no result from the superseded image set can be committed.

## Soak evidence

Run a bounded soak after the failure matrix and rollback test, for example:

```bash
... ./.venv/bin/python -m tools.staging_interface soak \
  --duration-seconds 3600 --interval-seconds 30
```

The interface checks each captured Compose JSON snapshot for at least one
running service and rejects stopped, malformed, or unhealthy snapshots before
continuing. It emits only a bounded service-snapshot count. The operator must
retain the external evidence needed to assess resource usage, restart counts,
diagnostic cardinality, reconciliation outcomes, worker capability state,
audio freshness, and stream continuity. The soak passes only when there is no
embedded bypass, resource leak, unbounded telemetry, stale-result promotion,
or repeated reconciliation failure.

## Acceptance boundary

P3-07 acceptance requires the staging evidence and repository checks to pass.
It does not authorize production migration, production restart, production
configuration mutation, or the P3-08 migration procedure and Phase 3 gate.
