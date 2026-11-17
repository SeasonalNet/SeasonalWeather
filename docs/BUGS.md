# Known bugs and operational risks

This page records confirmed defects and concrete operational failure paths.
The publication-lease repair below is implemented in the current tree; its
remaining operational gap is P3 staging validation. Potential defects that
still require a scenario-driven review are tracked separately in
[`REVIEW.md`](REVIEW.md).

## Scope

SeasonalWeather renders WAV files, sends their paths to Liquidsoap request
queues, and Liquidsoap encodes the selected source to Icecast. Icecast receives
the stream; Liquidsoap owns the request queue and is the authority for whether
an individual file is queued, prepared, on air, or finished.

The current Compose image is Liquidsoap 2.4.6. The P3 Compose topology shares
the same controller publication and cleanup code, so these risks apply to both
the Phase 4 integration line and the Phase 3 runtime-fixes line.

## Confirmed publication failure modes addressed below

### Queue admission is not playback completion

Before the publication-lease repair, the controller treated a successful
`push` command as enough evidence to update its own airing state. Liquidsoap's
request queues have a user-controlled queue and an internal prefetched queue. A
request can therefore be accepted by the telnet command while it is still
resolving, prepared but not on air, or waiting behind another request.

The pre-repair path was visible in:

- `seasonalweather/broadcast/conductor.py`, where scheduled inserts are pushed
  and immediately marked aired;
- `seasonalweather/database/inserts.py`, where a one-shot insert becomes
  `completed` at that point; and
- `seasonalweather/database/housekeeping.py`, which protects only active insert
  rows.

An old one-shot `insert_*.wav` can therefore lose its only durable reference
immediately after queue admission. The next housekeeping pass can unlink it
while Liquidsoap still has the request queued or prepared.

The same ownership gap applies to interrupt audio that expires or is removed
from the controller read model before Liquidsoap has started it.

### Superseded segment generations are not queue-pinned

Before publication leases, `SegmentStore` wrote immutable, versioned
`cycle_seg_*.wav` generations, but
its cleanup knows only about controller metadata and recovery journals. It
keeps two unreferenced generations and removes older ones. Liquidsoap request
IDs are not part of that reference inventory.

Three rapid refreshes can therefore remove the first generation even if its
pathname is still present in Liquidsoap's request queue.

### The configured audio grace is file age, not dereference age

`database.housekeeping.generated_audio_retention_seconds` is described as a
crash/race grace, but housekeeping compares the file's `mtime` with the cutoff.
It does not record when the final controller reference disappeared. A file
created an hour ago becomes immediately eligible once its database reference
is removed.

This makes the scheduled-insert and segment-generation races more likely than
the configuration comment suggests.

### Reused pathnames can change queued content

Live-time audio alternates between `cycle_time_0.wav` and `cycle_time_1.wav`.
The safety argument assumes a 15--30 minute cycle, but that is not enforced by
the queue. A short or degraded rotation can enqueue a later generation before
an earlier request using the same pathname has started.

Alert output also uses second-resolution names such as
`capvoice_YYYYMMDD-HHMMSS.wav` and `alert_YYYYMMDD-HHMMSS.wav`. Two same-prefix
renders in one second can target the same pathname.

### Queue reset is not automatically proof of an empty request pipeline

Resetting a request source clears the user-visible queue and may skip the
current source. Newer Liquidsoap documentation describes a separate
internal prefetched queue. Reset success must therefore not be treated as proof
that every request associated with the old cycle has reached a terminal state.

## Implemented fix: Liquidsoap-aware publication leases

The current tree uses a publication lease shared by the publisher,
reconciler, and housekeeping:

1. **Immutable artifact identity.** Give every pushed audio artifact a unique
   pathname. Do not reuse live-time buffers or second-resolution alert names.
2. **Request identity.** Each `sw.*.push` alias records the Liquidsoap request
   ID (RID) and `sw_pub_id` metadata. The 2.4.6 script registers
   `sw.publication.inventory`, which polls Liquidsoap's `request.all()` and
   `request.status(req)` APIs for the complete RID inventory and status map.
3. **Separate lifecycle states.** Track `submitted`, `queued`, `ready`,
   `on_air`, `finished`, `cancelled`, and `failed`. `submitted` or `queued` is
   never equivalent to `on_air`.
4. **On-air evidence.** Attach an output-level `on_track` callback and correlate
   `sw_pub_id`. Liquidsoap recommends output callbacks for actual on-air
   tracking; request-level `on_air` metadata is deprecated and can be
   misleading when sources are nested under `fallback`.
5. **Fail-closed cleanup.** Housekeeping protects a file while its RID is alive
   or its lease has not expired. Unknown Liquidsoap state retains the file for
   reconciliation instead of deleting it.
6. **Explicit cancellation.** On cycle reset or alert cancellation, record the
   cancellation, reset the relevant Liquidsoap request source, and retain the
   file until inventory polling reports the RID gone. Queue removal is a
   control operation, not by itself a filesystem-deletion proof.
7. **Restart reconciliation.** On controller or Liquidsoap restart, rebuild the
   lease inventory from durable publication records and the current Liquidsoap
   request/status commands. Keep recent ambiguous files until reconciliation is
   complete.

## Version and rollout plan

1. **Implemented:** the Compose default is
   `savonet/liquidsoap:v2.4.6`; do not use the 2.5 release-candidate line for
   the first production migration.
2. **Implemented:** RID-returning publication aliases, metadata correlation,
   complete inventory/status polling, and output `on_track` telemetry.
3. **Implemented:** durable publication leases are honored by housekeeping and
   segment-generation cleanup.
4. **Implemented:** pushed audio uses immutable unique pathnames.
5. **Remaining:** exercise queue backlog, prefetch, interruption, cancellation,
   restart, and cleanup races in the P3 staging stack before enabling aggressive
   pruning in production.

## Acceptance criteria

- A successful telnet push creates a durable publication record with an RID and
  immutable artifact path.
- Controller state distinguishes queue admission from actual on-air evidence.
- A file remains present while its RID is queued, resolving, ready, or playing.
- A completed/cancelled insert cannot be deleted until its Liquidsoap request is
  terminal and the lease grace has elapsed.
- Superseded segment generations remain present while any Liquidsoap request
  still references them.
- A reset or restart with ambiguous Liquidsoap state retains files and emits a
  reconciliation diagnostic rather than deleting them.
- Two pushes cannot overwrite one another's pathname.
- P3 staging proves the same behavior on the shared Phase 3 publication and
  housekeeping path. This is the remaining acceptance item; it is not claimed
  by the unit and container checks in this repository.

## Upstream references

- [Liquidsoap requests and queue lifecycle](https://www.liquidsoap.info/doc-2.4.6/requests)
- [Liquidsoap request queue methods](https://www.liquidsoap.info/doc-2.4.6/reference/source-track-processing)
- [Liquidsoap request identity and status](https://www.liquidsoap.info/doc-2.4.6/reference/liquidsoap)
- [Liquidsoap 2.4.5 release notes](https://github.com/savonet/liquidsoap/releases/tag/v2.4.5)
- [Liquidsoap release matrix](https://github.com/savonet/liquidsoap)

## SWWP shutdown can lose result-settlement evidence

Applies to both the Phase 4 integration line and the Phase 3 runtime-fixes
line.

Controller shutdown calls `LiveWorkerSessionManager.drain()`, but that method
only sends `Drain`. It does not wait for `Drained`, active assignments,
unacknowledged completions, or durable result settlement before supervisor
teardown can close the WebSocket.

Worker shutdown has the corresponding ordering problem. `WorkerRuntime.stop()`
closes the SWWP connection before `run()` awaits assignment tasks. A handler
that finishes during that interval may be unable to send its result or failure.
`WorkerRuntime._execute()` also removes runtime assignment tracking immediately
after sending a result, before the controller returns `ResultCommitted`.
Health and drain logic can therefore report no active work while durable
acknowledgment is still outstanding.

The smallest complete repair must:

- keep result-sent assignments tracked until `ResultCommitted` or explicit
  reconciliation;
- wait for a bounded `Drained`/settlement condition before closing the
  transport; and
- preserve replay-safe completion evidence when either side disconnects during
  shutdown.

Validation must interrupt both controller and worker at every transition from
assignment through result commitment, including reconnect to a new controller
epoch.

## Station-feed housekeeping is outside lifecycle supervision

Applies to both modernization lines.

`seasonalweather.broadcast.station_feed_runtime` starts housekeeping in a
daemon thread with an unconditional loop. It retains neither a stop event nor
a thread handle and has no join path. The thread can continue pruning the
in-memory feed and SQLite read model while controller shutdown checkpoints and
closes persistence resources.

Move the loop under the existing controller lifecycle/supervisor boundary with
bounded stop and join behavior. Validate shutdown during a housekeeping tick,
SQLite lock contention, and configuration reload/restart.

## Optional station-feed NWS fetching blocks and can silently be unavailable

Applies to both modernization lines when `station_feed.fetch_nws` is enabled.

Station-feed alert handling calls synchronous `requests.get()` with an
eight-second timeout in the alert path. `requests` is not a declared runtime
dependency. A minimal supported installation therefore raises `ImportError`,
which the broad exception handler silently discards. When `requests` happens to
be installed, a slow NWS response can block controller event-loop work for the
request timeout.

Use the controller's existing asynchronous HTTP boundary and emit bounded
failure evidence instead of silently disabling the option. Validate missing
dependencies, timeout, malformed JSON, oversized responses, cancellation, and
normal alert handling while the NWS endpoint is slow.

## File-backed Compose secrets can be unreadable to non-root services

Applies to both modernization lines when using ordinary Docker Compose
file-backed secrets.

The controller and workers run as UID/GID `10001:10001`, while the operator
instructions create host secret files with mode `0400`. The `uid`, `gid`, and
`mode` fields on file-backed Compose secrets are not honored by ordinary
Compose implementations that bind-mount the source file. A disposable non-root
probe reproduced a host-owned `uid=1000 gid=1000 mode=400` secret and
`Permission denied` inside a UID 10001 container. This can prevent the
controller from reading `ICECAST_SOURCE_PASSWORD` and workers from reading
`SEASONAL_WORKER_TOKEN`.

The deployment contract must make the mounted files readable by the intended
service identity without broadening access to other users. Repeat the probe for
the supported Compose implementation and for every service secret before
accepting a repair.

## Compose can terminate a valid controller drain early

Applies to both modernization lines.

The application lifecycle permits a bounded shutdown of up to 30 seconds, but
`compose.yaml` declares no matching `stop_grace_period`. A container runtime
may send `SIGKILL` before job reconciliation, SQLite checkpointing, executor
cleanup, worker settlement, or lifecycle-marker finalization completes.

Align the Compose stop grace with the application lifecycle budget and validate
both normal and forced shutdown with active requests, publications, and worker
assignments.

## Phase 3 staging omits shipped worker profiles

Applies only to `modernization/phase-3-runtime-fixes`.

The Phase 3 Compose graph defines espeak, Festival, and DECtalk worker
profiles, but its `tools/staging_interface.py` does not include those profiles
or their image variables in `OPTIONAL_PROFILES`, `PROFILE_IMAGE_KEYS`, or the
rollback image allowlist. The staging command therefore rejects profiles that
the same branch ships, and rollback validation cannot pin their images.

Bring the staging and rollback profile inventories into exact parity with the
Compose worker graph and add one exhaustive inventory test so later profiles
cannot drift silently.

## PostgreSQL startup and migration waits are not fully bounded

Applies only to the Phase 4 integration line.

`PostgresPreflight.run()` wraps synchronous psycopg work in
`asyncio.wait_for(asyncio.to_thread(...))`. Timing out the await does not stop
the underlying connection or query. Only connection establishment has a libpq
timeout; subsequent preflight queries have no statement or transaction timeout.
The migration runner can likewise wait indefinitely on
`pg_advisory_xact_lock`.

A stalled database operation can continue after startup reports the optional
archive unavailable and can later delay default-executor shutdown. Apply
database-side statement, lock, and transaction bounds and validate a server
that stalls after connection, advisory-lock contention, cancellation, and
shutdown after an apparent preflight timeout.
