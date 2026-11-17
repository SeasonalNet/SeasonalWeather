# Known audio publication and cleanup risks

This page records a confirmed controller/Liquidsoap ownership gap. It is a
design and follow-up plan, not a claim that the fixes below are already
implemented.

## Scope

SeasonalWeather renders WAV files, sends their paths to Liquidsoap request
queues, and Liquidsoap encodes the selected source to Icecast. Icecast receives
the stream; Liquidsoap owns the request queue and is the authority for whether
an individual file is queued, prepared, on air, or finished.

The current Compose image is Liquidsoap 2.3.3. The P3 Compose topology shares
the same controller publication and cleanup code, so these risks apply to both
the Phase 4 integration line and the Phase 3 runtime-fixes line.

## Confirmed failure modes

### Queue admission is not playback completion

The controller currently treats a successful `push` command as enough evidence
to update its own airing state. Liquidsoap's request queues have a
user-controlled queue and an internal prefetched queue. A request can therefore
be accepted by the telnet command while it is still resolving, prepared but not
on air, or waiting behind another request.

The current path is visible in:

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

`SegmentStore` writes immutable, versioned `cycle_seg_*.wav` generations, but
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

The current `sw.cycle.reset` handler clears the user-visible queue and skips
the current source. Newer Liquidsoap documentation describes a separate
internal prefetched queue. Reset success must therefore not be treated as proof
that every request associated with the old cycle has reached a terminal state.

## Planned fix: Liquidsoap-aware publication leases

The smallest safe design is a publication lease shared by the publisher,
reconciler, and housekeeping:

1. **Immutable artifact identity.** Give every pushed audio artifact a unique
   pathname. Do not reuse live-time buffers or second-resolution alert names.
2. **Request identity.** Add a `sw_pub_id` metadata value and make each
   `sw.*.push` alias return the Liquidsoap request ID (RID) as well as accepting
   the request. Liquidsoap exposes `request.id`, `request.status`,
   `request.filename`, and request inventories such as `request.alive` and
   `request.on_air`.
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
6. **Explicit cancellation.** On cycle reset or alert cancellation, remove the
   known request where supported, record the cancellation, and retain the file
   until Liquidsoap reports the request terminal. Queue removal is a control
   operation, not by itself a filesystem-deletion proof.
7. **Restart reconciliation.** On controller or Liquidsoap restart, rebuild the
   lease inventory from durable publication records and the current Liquidsoap
   request/status commands. Keep recent ambiguous files until reconciliation is
   complete.

## Version and rollout plan

1. Stage the upgrade from Liquidsoap 2.3.3 to the current stable 2.4.x image
   (`savonet/liquidsoap:v2.4.6`). Do not use the 2.5 release-candidate line for
   the first production migration.
2. Add RID-returning push aliases, metadata correlation, status polling, and
   output `on_track` telemetry without changing deletion policy.
3. Add the durable publication-lease record and make housekeeping and segment
   generation cleanup honor it.
4. Replace fixed/reused output names with immutable unique names.
5. Exercise queue backlog, prefetch, interruption, cancellation, restart, and
   cleanup races in the P3 staging stack before enabling aggressive pruning.

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
  housekeeping path.

## Upstream references

- [Liquidsoap requests and queue lifecycle](https://www.liquidsoap.info/doc-2.4.6/requests)
- [Liquidsoap request queue methods](https://www.liquidsoap.info/doc-2.4.6/reference/source-track-processing)
- [Liquidsoap request identity and status](https://www.liquidsoap.info/doc-2.4.6/reference/liquidsoap)
- [Liquidsoap 2.4.5 release notes](https://github.com/savonet/liquidsoap/releases/tag/v2.4.5)
- [Liquidsoap release matrix](https://github.com/savonet/liquidsoap)
