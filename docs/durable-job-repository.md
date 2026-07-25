# Durable controller-owned jobs

SeasonalWeather has a controller-owned SQLite repository for bounded jobs. It
is a persistence and scheduling foundation: it does not run handlers, publish
artifacts, mutate Liquidsoap, or turn long-running sources into jobs.

## Ownership and database separation

`seasonalweather.job_store` is the only owner of job persistence, lease
coordination, retry/deadline/cancellation decisions, deduplication,
coalescing, durable result commitment, and restart reconciliation.

Jobs use a separate database configured under `jobs`. The operational
`database.path` continues to own `api_commands`, alerts, station-feed state,
cycle inserts, and other controller state. The two SQLite files never
participate in a claimed atomic transaction. `jobs.path` is required when the
repository is enabled and must resolve to a different path.

The job database is supported only on a local filesystem. NFS and SMB are not
supported. Every connection enables foreign keys, WAL, and the configured
bounded busy timeout. The repository uses short explicit transactions and
parameterized SQL. It performs no network, TTS, artifact/filesystem, handler,
or sleep operation inside a transaction.

The job schema has an independent `job_schema_migrations` history. Startup
initialization and migrations are idempotent. Shutdown performs bounded lease
reconciliation followed by a passive WAL checkpoint; connections are otherwise
short-lived.

## Durable model

The schema records:

- current job metadata, state, policy identity, schema versions, timing,
  configuration generation, cancellation state, and optimistic version;
- immutable, monotonically numbered attempts;
- current lease/assignment-ack identity and bounded lease evidence;
- bounded structured progress and repository events;
- dedupe, coalescing, and supersession relationships;
- bounded terminal result/error metadata and an immutable result-commit hash.

Payloads and results are deterministic JSON with configured byte limits.
Registry schemas remain authoritative. The repository rejects credentials,
authorization material, raw exceptions/tracebacks, absolute paths, binary
objects, and excessive nesting or text. Audio blobs, source products, raw
synthesis prose, pickles, and active broadcast files do not belong in this
database; typed references are stored instead.

## Admission, deduplication, and coalescing

`DurableJobService` applies admission in this order:

1. the controller lifecycle command gate;
2. the P1-06 job registry, payload, deadline, generation, replay, and static
   capability-contract validation;
3. one atomic repository transaction for dedupe/coalescing/supersession and
   durable insertion.

Typed dispositions are `created`, `reused`, `coalesced`, `superseded`, and
`conflict`; validation or a closed lifecycle rejects before durable commit.
Concurrent exact admission yields one authoritative row. Exact dedupe never
coalesces a different safety-critical source/event/content identity.
Coalesce-latest policy may supersede an older pending specification. It does
not steal or silently rewrite an already leased/running attempt.

## Scheduler and leases

`JobScheduler` is the controller queue authority. Its deterministic `assign`
method filters pending jobs by queue, executor class, priority, `not_before`,
deadline, cancellation state, and declared static capability names, then
atomically leases one job. It returns a typed `JobAssignment` to a future
consumer port and invokes no handler.

P1-09 adds a narrow qualified-assignment input. Its ephemeral capability
service first evaluates and reserves a worker snapshot, then asks this
repository to acquire only the selected job ID. `candidate_job_ids=None` means
no additional ID restriction, while an explicitly empty candidate collection
means no job is eligible. The repository still rechecks static capability names
and all durable eligibility predicates atomically. The reservation is never
persisted here and cannot create a job or lease.

Each assignment has an opaque lease ID, a distinct attempt ID, positive
monotonic attempt number, bounded owner, assignment acknowledgment deadline,
and lease expiry capped by the absolute job deadline. Acknowledgment starts
the attempt. Renewal, progress, cancellation acknowledgment, and outcomes must
match job, controller instance, lease, owner, and attempt. State/version
predicates and checked row counts reject stale or concurrent last-write-wins
updates.

Retries are scheduled only by the P1-06 retry policy, remaining attempts,
deadline, cancellation state, failure category, replay evidence, and bounded
backoff. Prior attempts are never overwritten.

## Progress, cancellation, and result commitment

Progress accepts only bounded stage/reason keys and numeric values under the
current running lease. Retention is per job and deterministic.

Pending cancellation becomes terminal immediately. Leased or running
cancellation records `cancel_requested`; transport delivery alone is not
completion. A current attempt may acknowledge cancellation. A successful
result racing with a cancellation request fails closed.

Successful results are validated against the registered result schema and
byte bound, hashed, and committed in the same transaction as the terminal job
transition and attempt outcome. An identical committed result can be replayed
idempotently; a different or stale result is rejected. This receipt proves
only durable job result commitment. It does not prove artifact publication,
Liquidsoap mutation, controller finalization, or command completion.

## Restart and command consistency

Initialization reconciles before admission or leasing opens. Reconciliation
is bounded and idempotent:

- deadlines become expired;
- unacknowledged assignments are released or fail on attempt exhaustion;
- an expired or prior-controller running lease becomes a bounded
  `revalidation_required` decision rather than a blind replay;
- cancellation and pending command aggregation remain visible;
- attempt and lease evidence is preserved.

Restart alone proves neither success nor safe replay. Safety-critical or
ambiguous effects require later authoritative controller revalidation.

Job state and command state deliberately live in separate databases.
`CommandJobCoordinator` repairs the crash window after a durable job
transition by rebuilding command aggregation from job rows and applying an
idempotent command update afterward. Controller-finalization relationships
remain running after child success until their explicit controller authority
finishes them.

## Lifecycle and health

Drain immediately closes job admission and lease acquisition through P1-05
`Lifecycle`. Read-only inspection remains available. Shutdown reconciles
current leases within `jobs.shutdown_reconciliation_seconds` and checkpoints
the job database. This packet uses deterministic scheduler calls and therefore
adds no background scheduler task; any future loop must be registered with the
controller task supervisor.

`/healthz` is unchanged. Detailed health reports only bounded repository
state: enabled/initialized/schema/WAL/admission status, queue counts, active
leases, overdue jobs, cancellation backlog, and reconciliation-required count.
No path, SQL, payload/result, actor, lease owner, or credential is exposed.
Readiness requires this component only when `jobs.required` is true.

## Configuration

```yaml
jobs:
  enabled: false
  required: false
  path: "/var/lib/seasonalweather/jobs.sqlite3"
  busy_timeout_ms: 5000
  lease_seconds: 60
  assignment_ack_seconds: 10
  progress_retention: 100
  event_retention: 500
  reconciliation_batch_size: 100
  payload_max_bytes: 65536
  result_max_bytes: 65536
  shutdown_reconciliation_seconds: 5.0
```

These are restart-time settings. Editing the repository example does not
change `/etc/seasonalweather/config.yaml`.

## Deferred boundaries

P1-07 deliberately does not own SWWP. P1-08 adds a separate
`seasonalweather.swwp` package whose sole concrete adapter calls these public
repository/scheduler ports; its schemas, session machines, and test-only
simulated peers are documented in [`swwp.md`](swwp.md). There is still no live
connection, worker handler/process, embedded production executor, artifact
staging or promotion, active-file mutation, result fencing/publication,
PostgreSQL, Redis, container, or deployment behavior. P1-09 dynamic capability
state remains simulated-only and ephemeral.
