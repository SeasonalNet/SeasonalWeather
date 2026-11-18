# Known bugs and operational risks

This page records confirmed defects, residual operational risks, and explicit
acceptance gaps. Addressed items remain here as audit history, with their
current status stated in the heading. Potential defects that still require a
scenario-driven review are tracked separately in
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

## SWWP reconnect settlement requires a live transport scenario

The current tree keeps result-sent assignments until `ResultCommitted`, waits
for bounded drain/settlement conditions, and carries completion evidence into
reconnect reconciliation. A disconnect between `JobResult` and
`ResultCommitted` still requires a live controller/worker transport scenario to
confirm every reconciliation disposition and shutdown interaction.

This is therefore an unconfirmed review scenario, not a reproduced defect in
the current tree. Keep the scenario in [`REVIEW.md`](REVIEW.md) until it can be
exercised against the supported transport without relying only on static
inspection or a forced unit-test failure.

## Station-feed housekeeping lifecycle is addressed

Applies to both modernization lines.

Housekeeping now runs as a supervised asyncio task with a stop event and
bounded supervisor cancellation. The remaining work is live/scenario
validation during a housekeeping tick, SQLite lock contention, and
configuration reload/restart; the former daemon-thread defect is addressed.

## Optional station-feed NWS fetching blocking/import issue is addressed

Applies to both modernization lines when `station_feed.fetch_nws` is enabled.

Station-feed alert handling now uses the declared `httpx2` dependency through
the controller's asynchronous NWS API boundary, so the old missing-`requests`
and event-loop blocking paths are addressed. Remaining validation covers
timeouts, malformed or oversized responses, cancellation, and normal alert
handling while the NWS endpoint is slow.

## File-backed Compose secret ownership contract clarified

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

The deployment instructions require mode `0400` and ownership mapped to
container UID/GID `10001:10001` for the controller, all workers, and Liquidsoap.
A disposable Docker Compose v5.7.1 probe confirmed all three mounted files
were readable with that ownership and mode under rootless Docker.

## Compose stop grace for controller drain is addressed

Applies to both modernization lines.

The application lifecycle permits a bounded shutdown of up to 30 seconds and
`compose.yaml` now declares a 35-second `stop_grace_period`. The configuration
gap is addressed; live validation with active requests, publications, and
worker assignments remains an operator acceptance check.

## Phase 3 staging profile inventory is addressed

Applies only to `modernization/phase-3-runtime-fixes`.

Staging and rollback now include the espeak, Festival, and DECtalk profiles
shipped in the Phase 3 Compose graph. An inventory test compares the staging
profiles and rollback image variables against every Compose worker profile.

## PostgreSQL startup and migration cancellation remains a residual risk

Applies only to the Phase 4 integration line.

The PostgreSQL connector now applies bounded `statement_timeout`,
`lock_timeout`, and `idle_in_transaction_session_timeout` values, so the old
claim of unbounded queries and advisory-lock waits is no longer current.
However, `PostgresPreflight.run()` still wraps synchronous psycopg work in
`asyncio.wait_for(asyncio.to_thread(...))`. If the outer wait expires, it
cancels only the asyncio wait; the underlying thread keeps its connection and
does not reach its `close()` path until the database-side timeout or operation
returns.

This is a bounded cancellation/resource-retention risk, not an unbounded hang.
It can temporarily occupy the default executor and retain a database session
after startup has already reported PostgreSQL unavailable. Validate the
behavior with a server that stalls after connection, advisory-lock contention,
cancellation, and shutdown after an apparent preflight timeout.

## Review-pass findings addressed in the current tree

### SAME decoder output could grow without bound

The `samedec` listener accumulated decoder stdout until it saw a newline. A
decoder or damaged pipe that emitted an unterminated line could therefore grow
the controller-side listener buffer without a byte bound. The listener now
discards an overlong unterminated line at 64 KiB and has a regression test.

### Uploaded-audio normalization could block the API event loop

The authenticated audio-upload route called FFmpeg through the synchronous
`AudioAssetService._normalize()` method from an async request handler. A slow or
hostile input could therefore block unrelated API requests while normalization
ran. Normalization now runs through the controller's default worker thread and
the upload remains bounded before staging.

### Concurrent TTS finalization was only locally serialized

The async TTS bridge created its publication lock inside each invocation. Two
requests targeting one output path could consequently enter the final replace
section concurrently. Publication now uses one process-wide short critical
section, preserving atomic replacement while preventing interleaved finalizers.

### Compose crash and reboot recovery had no declared owner

The supported Compose deployment had healthchecks but no restart policy, so a
crashed service remained stopped after a host or daemon restart. The shared
service policy now declares `restart: unless-stopped`, covering crash and
reboot recovery. Compose healthchecks remain observation-only: an unhealthy
container that is still running still requires an operator or external health
supervisor to restart it.

## Aggressive review findings addressed

These fixes apply to both modernization lines. The upload cancellation
finding applies to Phase 4.

### Mixed NWWS WCN retry preserves FULL admission

The active FULL and terminal VOICE portions have separate deduplication keys.
A failed terminal render or push releases its reservation. Retrying the same
product queues the terminal voice while retaining the admitted FULL tone-out.

### Controller reconciles interrupted artifact publications on startup

Startup reconciles receipts using durable job and active-file evidence.
Unpublished receipts require a current, fenced result before a replacement
can proceed. The retry checks the prior target digest under the promotion lock
and refuses to overwrite a different publication.

### TTS deadlines cover descendant-held pipes

The deadline covers subprocess exit and all three pipe owners. On timeout or
cancellation, cleanup signals the process group even if its leader already
exited, escalates to kill, and bounds its waits.

### Cancelled audio uploads relinquish unpublished files

The executor owns the staged input until normalization finishes. Cancellation
schedules deletion of the unpublished output when normalization settles.

### SAME decoder stdout drains are bounded

The read loop caps work per drain and discards an oversized line through its
newline, including a suffix received during a later drain.

### Staging soak requires every selected service

The soak includes stopped containers in the Compose snapshot and requires
the controller, routine worker, Liquidsoap, Icecast, and selected workers.
Services with healthchecks must report healthy.

### Reload retirement propagates cancellation after commit

Cancellation preserves the committed generation and pending retirement evidence
before propagating. Recovery can finish retirement and finalize the command.
