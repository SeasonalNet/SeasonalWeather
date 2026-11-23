# Phase 4 acceptance evidence

Revision 10 §33 is the acceptance checklist; §56 provides the integration and
failure-injection test strategy. Run `make phase4-ci` for the portable subset.
Both GitHub and Forgejo already run it as part of full pytest discovery via
`make check`; no container daemon is required for this subset.

| Criterion | Portable evidence | Live developer-gate result |
| --- | --- | --- |
| Station continues with PostgreSQL unavailable | Preflight degrades without a fatal error; outbox retains local state | Icecast decoded the distinct markers before, during, and after the outage, including after a controller restart with PostgreSQL still down |
| Replay is idempotent; reconnect catches up | Real SQLite outbox and simulated PostgreSQL service: outage, restart, partial remote batch, lost commit ACK, and multi-batch replay | The same snapshot UUID and payload survived the restart, reached real PostgreSQL, and was acknowledged in SQLite; the controller did not restart during recovery |
| Migrations repeat safely | Transaction/lock simulation, interruption, checksum drift, manual-only refusal | Startup migrated a fresh schema; the safe runner applied no new versions on the retained schema and left migration history unchanged |
| Duplicates reconcile; conflicts are preserved | Identity and reconciliation tests cover repeat and cross-source delivery and conflicting content | Covered by the portable suite; the live run did not inject duplicates or conflicts |
| Recovered records do not air | Recovery tests assert non-broadcastable provenance and archive-only persistence | The recovered fixture and its checkpoint were both non-broadcastable; the audio markers were separate |
| Archive and checkpoints agree | Stable checkpoint IDs, committed-version lookup, checkpoint repair, and same-ID correction coverage | The checkpoint linked to the expected forecast fixture written by bounded recovery into real PostgreSQL |
| PostgreSQL backup and restore (§56) | — | Restored a logical dump into a separate database and verified migration history, the replayed snapshot, the backfill pair, and PostGIS SRID 4326 |
| Ownership and quality remain intact | `make check` passed the quality and architecture ownership checks | No additional topology required |

Disk pressure uses a real SQLite `SQLITE_FULL` error, raised by a per-connection
page ceiling on a disposable database. It checks rollback of the failed update
and subsequent recovery without filling the developer's disk. The portable
suite is not a substitute for the [developer Docker outage and stream check](p4-07-docker-gate.md).

## Developer validation — 2026-11-23

- `make phase4-ci`: **34 passed**. `make check`: **1,606 passed**, including
  quality, architecture ownership, and compilation checks.
- Requester host: `make phase4-docker-gate` **passed in 1m 39s**, including
  cleanup of project `sw-p4-gate-i479vu0w`. The routine eSpeak worker rendered
  a non-placeholder station ID that the controller committed. All five MP3
  marker checks passed (440, 550, 660, 770, and 880 Hz). The outage snapshot
  `ce159238-046c-4456-9c63-e53c326f5001` was replayed and verified in the restore.

Together these runs satisfy the Revision 10 Phase 4 exit gate. The live check
samples audibility, not gap-free routine playback. It uses the routine worker
profile and a deterministic recovery response, not every TTS image or live NWS.
These are local results; no hosted CI or merge result is recorded here.
