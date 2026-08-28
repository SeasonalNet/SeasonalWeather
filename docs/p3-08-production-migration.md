# P3-08 production migration and Phase 3 exit gate

P3-08 is the production handoff boundary for the Phase 3 Compose topology. It
turns the accepted P3-07 staging evidence into a reversible production
procedure and records which parts of the §24 gate are repository checks,
staging evidence, or production-operator evidence.

This document is a procedure and acceptance contract. It does not authorize a
production restart, configuration mutation, host reboot, or live stream
cutover by itself. A migration must be scheduled and performed by the
deployment operator with the rollback material available before the cutover
begins.

## Ownership and target topology

The production Compose project owns the controller, the routine worker, the
selected optional workers, Liquidsoap, and Icecast when the `icecast` profile
is enabled. The controller remains the sole owner of durable state, jobs,
configuration-generation commits, artifact validation/promotion, and final
publication. Workers execute admitted jobs and write only to the shared
staging area. Liquidsoap reads controller-promoted artifacts and never reads
worker staging files.

The target project must have one controller and one Liquidsoap publisher. The
legacy systemd controller/Liquidsoap units and any host Icecast instance must
be stopped before their corresponding Compose services become authoritative;
running both paths would create duplicate schedulers or publishers. If a
deployment intentionally keeps host Icecast, it must omit the Compose
`icecast` profile and document that external authority before the change.

The source `systemd/` units remain supported source-deployment artifacts. They
are not an additional executor in a Compose deployment.

## Required inputs and preflight

The operator prepares these files outside the repository:

```text
/etc/seasonalweather/config.yaml
/etc/seasonalweather/seasonalweather.env
/etc/seasonalweather/compose.env
/etc/seasonalweather/secrets/ICECAST_SOURCE_PASSWORD
/etc/seasonalweather/secrets/SEASONAL_API_TOKEN
/etc/seasonalweather/secrets/SEASONAL_WORKER_TOKEN
```

`compose.env` contains paths, project settings, and immutable image
references, but no secret values. At minimum it binds
`SEASONALWEATHER_CONFIG_FILE`, `SEASONALWEATHER_SECRET_DIR`,
`SEASONALWEATHER_ICECAST_CONFIG_FILE`, and the controller, routine-worker,
Liquidsoap, and Icecast image variables to `image@sha256:<64 lowercase hex>`.
Optional worker image variables are included when their profiles are enabled.
The config and secret paths must be absolute deployment paths; credentials are
not printed into logs or migration evidence.

Before the maintenance window:

1. Confirm P3-07 staging evidence, the accepted commit, the named Forgejo
   result, and the matching Phase 2 image/build identity.
2. Run the configuration compiler and validator against the candidate live
   configuration. Use the configuration reload dry run for a behavioral
   change and retain its redacted report. A restart-required result is not a
   live change.
3. Check the target image references and Compose graph without starting it:

   ```bash
   docker compose --project-name seasonalweather \
     --env-file /etc/seasonalweather/compose.env \
     --file /opt/seasonalweather/app/compose.yaml \
     --profile icecast config --quiet
   ```

4. Verify that all configured secret files are regular files with mode `0400`
   (or stricter), that the runtime user can read them, and that the live config
   is not the repository example.
5. Record the current image digests, Compose service IDs, readiness result,
   worker qualification/capabilities, current configuration generation, and
   current stream/Now Playing observation.
6. Take a restorable backup of the live configuration, the controller state
   database, the durable jobs database, and the accepted artifact metadata.
   Use SQLite's online backup facility while the service is live, or stop the
   writer first and preserve the database's `-wal` and `-shm` companions. Do
   not copy an active SQLite main file alone and call that a backup.
7. Confirm that the previous immutable image-set env file and the previous
   configuration are accessible from the host. Rollback must not depend on a
   registry tag that can move.

The preflight stops on an invalid config, missing worker capability required by
the selected TTS mode, mutable image reference, unavailable backup, or
ambiguous current publisher ownership.

## Cutover procedure

The following commands are a controlled example. The operator substitutes the
approved paths and profile set; commands are not run by the repository gate.

1. Announce the maintenance window and close new production control changes.
   Do not start an unrelated configuration reload during the cutover.
2. Drain the existing controller through its normal lifecycle path, then stop
   the old controller and publisher. Stop host Icecast only if Compose Icecast
   is the selected authority. Do not remove volumes:

   ```bash
   sudo systemctl stop seasonalweather.service
   sudo systemctl stop seasonalweather-liquidsoap.service
   docker compose --project-name seasonalweather \
     --env-file /etc/seasonalweather/compose.env \
     --file /opt/seasonalweather/app/compose.yaml \
     --profile icecast down --remove-orphans
   ```

   Repeat `--profile PROFILE` for every optional worker profile enabled by the
   deployment. If a remote TTS mode is selected, include its corresponding
   Compose overlay file in every `config`, `down`, and `up` command.

3. Re-run the Compose `config --quiet` check after the stop and verify that the
   environment still resolves to the intended immutable image set and external
   live configuration.
4. Start the target graph. Include `--profile icecast` and each approved
   optional worker profile, or omit `icecast` when host Icecast remains the
   declared authority:

   ```bash
   docker compose --project-name seasonalweather \
     --env-file /etc/seasonalweather/compose.env \
     --file /opt/seasonalweather/app/compose.yaml \
     --profile icecast up --detach --force-recreate --remove-orphans
   ```

5. Wait for controller readiness and worker liveness. Verify the routine worker
   is registered through authenticated outbound SWWP, required capabilities
   are qualified and fresh, no worker has a published controller-facing port,
   and no worker can access controller state, jobs, or logs.
6. Verify that the controller sees the prior durable state and configuration
   generation, that pending jobs reconcile without duplicate execution, and
   that the controller can validate/promote a newly generated artifact.
7. Verify that Liquidsoap reads the newly promoted artifact, the selected
   Icecast surface remains connected, and the stream/Now Playing metadata is
   current. Use an approved bounded test or scheduled test window; do not
   inject unapproved audio into the live stream.
8. Keep the previous image env file, backup identities, service snapshots, and
   post-cutover health evidence with the migration record. Monitor the bounded
   failure and diagnostic surfaces through the agreed observation window.

If any step fails, stop admitting new work, preserve the evidence, and use the
rollback procedure. Do not repeatedly recreate a failing production graph
without deciding whether the failure is an image, configuration, dependency,
worker, or data-reconciliation failure.

## Configuration reload during and after migration

Configuration changes continue to use the controller-owned reload protocol.
The candidate is captured, compiled, semantically validated, independently
verified, and admitted with its source identity and report before activation.
Quiescent changes wait for the safe point covering alert origination, TTS,
artifact promotion, segment refresh, conductor mutation, and lifecycle drain.
Restart-required changes remain report-only and are applied by a planned
Compose recreation using the migration procedure.

Station identity (`organization_name`, `service_name`, Now Playing metadata),
the primary WFO, TTS selection, worker restart policy, endpoints, secrets,
state paths, and process topology are deployment configuration. They must not
be changed by editing Python source or by mutating a running container. A
successful generation commit fences older jobs and worker results; a stale or
ambiguous result cannot replace the current artifact.

## TTS mode and worker checks

Each configured mode is accepted independently; enabling one mode does not
silently install or select another:

| Mode | Production graph | Required proof |
| --- | --- | --- |
| Local-only | Base Compose plus the selected local worker profile | Qualified TTS and alert-artifact capabilities, successful controller-owned validation and promotion, and safe worker-loss readiness failure |
| `seasonal_ttsd` | Base Compose plus the controller-only external-provider overlay | Credential/TLS admission, bounded synthesis, controller validation/promotion, and a provider-outage fallback or last-known-good result when policy permits |
| OpenAI-compatible | Base Compose plus the controller-only external-provider overlay | Provider contract and response bounds, controller validation/promotion, and safe provider failure/fallback behavior |

Remote-provider overlays never give provider credentials to workers or add a
provider-side publication path. Remote failure must not remove a qualified
local capability. Cancellation and an expired overall deadline do not start a
new fallback synthesis. The selected mode, fallback policy, profile, and
credential path are recorded as redacted configuration identities.

## Rollback

Rollback is an image-set and configuration operation, not a volume reset:

1. Close admission and drain the target controller. Preserve logs, health
   output, image identities, configuration-generation evidence, and any
   reconciliation-required result.
2. Stop the target Compose services without `-v`, `volume rm`, or any operation
   that removes state, jobs, artifacts, staging, logs, or voice volumes.
3. Restore the previously accepted immutable image env file and, when the
   candidate configuration is implicated, restore the previously backed-up
   live configuration with its original restrictive ownership and mode.
4. Re-run `config --quiet`, then recreate the same service/profile set with
   the previous immutable references:

   ```bash
   docker compose --project-name seasonalweather \
     --env-file /etc/seasonalweather/compose-previous.env \
     --file /opt/seasonalweather/app/compose.yaml \
     --profile icecast up --detach --force-recreate --remove-orphans
   ```

5. Verify readiness, worker qualification, durable state, current generation,
   artifact fencing, Liquidsoap visibility, and stream continuity. The prior
   controller-accepted artifact remains authoritative until a new result is
   independently accepted.

Do not restore a database over post-cutover writes merely because an image
rollback failed. Database restoration is a separately approved data-recovery
operation and requires the service stopped, an identified backup, and an
explicit reconciliation decision.

## Observability and failure handling

The migration record retains bounded, secret-free evidence: build/image
identities, config/report/diff identities, service snapshots, readiness and
worker capability states, configuration generation, job reconciliation counts,
artifact acceptance/promotion outcomes, diagnostic codes, and stream checks.
It does not retain raw config, source payloads, credentials, authorization
headers, provider bodies, or arbitrary exception text.

| Failure | Expected handling | Migration decision |
| --- | --- | --- |
| Compose config or image identity rejected | Do not start; correct the external input | Abort before cutover |
| Controller readiness unavailable | Preserve old image/config evidence and inspect bounded readiness diagnostics | Roll back or defer |
| Worker absent, incompatible, stale, or unqualified | Controller remains fail-closed for worker-owned jobs; no controller bypass | Restore worker or roll back |
| Lease/assignment/reconciliation failure | Durable state remains controller-owned and visible | Stop admission; reconcile before retry |
| TTS provider/engine outage | Use configured permissible fallback or last-known-good audio; never publish unvalidated output | Continue only if readiness and deadline policy allow |
| Stale worker result or generation/source mismatch | Reject and retain the current artifact | Continue after evidence, or roll back |
| Liquidsoap/Icecast cannot read or publish current audio | Keep the last accepted artifact; do not delete it as recovery | Roll back before stream cutover |
| Optional supervised task exits | Record degraded diagnostics; apply configured `never`, bounded `restart`, or cooldown-aware `always` policy; drain/cancel suppresses restart | Investigate thrash before repeated recovery |
| SQLite busy/reconciliation ambiguity | Preserve the database and WAL evidence; do not declare success from a partial copy | Defer or use approved data recovery |
| Host reboot or service recreation | Re-check all readiness, worker, persistence, artifact, and stream criteria | Pass only with post-reboot evidence |

## Phase 3 exit gate

The repository source gate is:

```bash
make PYTHON=./.venv/bin/python phase3-gate
```

It runs the governed quality checks, compilation, complete test suite, and
checked-in Compose syntax validation. The Phase 2 image gate remains required
for the image matrix:

```bash
make PYTHON=./.venv/bin/python phase2-gate
```

P3-07 supplies the isolated staging evidence and bounded soak/failure matrix;
its interface must be used with external staging config and secrets. The
following table is the §24 completion checklist. A green source gate alone
does not mark the operator-only rows complete.

| §24 criterion | Evidence authority |
| --- | --- |
| Compose ordering, service roles, worker-only execution, outbound SWWP, no worker ports | `make phase3-gate`, P3-01/P3-02 tests, Compose inspection |
| Capability updates, probes, epochs/digests, validity, capacity, last-moment rejection | worker capability and SWWP tests plus P3-07 failure matrix |
| Host reboot and service recreation preserve state | P3-07 staging reboot/recreate evidence and production migration record |
| Liquidsoap reads newly generated audio | P3-07 alternate-stream test and production stream check |
| Local-only, `seasonal_ttsd`, and OpenAI-compatible modes | P3-03/P3-04/P3-05 tests and configured-mode staging evidence |
| Remote failure, fallback, backend switching, and rollback | TTS integration tests plus bounded staging failure evidence |
| Stale worker/config/source/event results are rejected | artifact, job, and configuration-reload integration tests |
| Worker artifacts stage and controller alone promotes | P3-02 and artifact-result integration tests |
| Validation jobs and final process commits work end to end | configuration-validation and configuration-reload integration tests |
| Image rollback is tested | P3-07 immutable rollback evidence and migration record |
| Observability and failure handling are bounded | diagnostics, health, lifecycle, and worker-runtime tests plus migration evidence |
| No service exceeds its declared authority/failure domain | architecture check and complete Phase 3 test suite |

P3-08 is complete only when every row has its corresponding evidence, the
named CI result is green, the migration procedure has operator review, and the
production cutover/rollback record is retained. No production migration has
been performed by this repository change.
