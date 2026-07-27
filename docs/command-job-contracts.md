# Command and job contracts

SeasonalWeather distinguishes requested outcomes from bounded execution:

- A **command** is an authorized external or internal request. It records the
  actor, reason, idempotency identity, audit context, state, and final bounded
  result or error.
- A **job** is an immutable, typed specification for one bounded execution
  unit. A job may belong to a command, while internal maintenance jobs may have
  no command.
- Long-running NWWS-OI, CAP, IPAWS, NWS, and ERN source services are lifecycle-
  supervised services. They are not jobs.

The contracts under `seasonalweather.commands` and `seasonalweather.jobs` are
persistence-neutral. The command application service adapts typed commands to
the operational controller database. The separate controller-owned durable job
repository and non-executing scheduler are documented in
[`durable-job-repository.md`](durable-job-repository.md).

## Command contract

Command types are a closed vocabulary matching the current API operations.
Command state is independent of job state:

```text
accepted -> running -> succeeded
    |          |    -> failed
    |          |    -> cancelled
    |          |    -> expired
    |          |    -> superseded
    +----------+----> failed/cancelled/expired/superseded
```

Terminal states are immutable. Repeating the same transition is idempotent;
other invalid transitions fail closed. A cancellation request records
`cancel_requested_at` without claiming terminal cancellation. Timestamps are
timezone-aware UTC and must be chronologically consistent.

Command results and errors have bounded codes, messages, references, and JSON
details. Actor, reason, request/correlation identity, idempotency key, and audit
context are bounded. Credentials, authorization material, raw synthesis text,
raw source payloads, tracebacks, and filesystem paths are prohibited.

API idempotency compares the command type and a deterministic request hash.
The raw request is not retained in the command record or `api_commands` row.
Reusing a key with the same request returns the original command; reusing it
with a different request is a conflict.

### Command-to-job relationships

Relationships declare one of:

- `no_jobs`: a controller operation completes without jobs;
- `all_required_jobs`: all required children must finish under the command's
  completion policy; optional child failure alone does not fail the command;
- `controller_finalization`: required child results still need authoritative
  controller acceptance or publication.

Internal jobs may omit `command_id`. Job submission is not command completion,
worker success is not artifact publication, and one successful child is not
multi-job command success. Cancellation propagation applies only to declared
required jobs and cannot reverse a terminal child or command.

## Job contract

Job states are:

```text
pending -> leased -> running -> succeeded
   |          |         |    -> failed
   |          |         |    -> cancelled
   +----------+---------+----> expired
   +----------+---------+----> superseded
```

The durable repository uses this lease identity to reject stale attempt
updates and implements acquisition, renewal, and recovery. An actual
attempt receives a positive, monotonically increasing attempt number and a new
attempt identity. Lease owner, attempt identity, lease expiry, deadline, and
state must all match before start or completion.

Attempt outcomes are `succeeded`, `retryable_failure`, `permanent_failure`,
`cancelled`, `timed_out`, and `lost`. Job state and attempt outcome are
separate. Retry is allowed only when the declared failure category, maximum
attempts, cancellation state, backoff, and deadline all permit it. Timeout is
not retryable by default. An uncertain safety-critical side effect is never a
blind retry category.

Replay is distinct from retry:

- `never`: uncertain completion is not replayed;
- `authoritative_revalidation`: replay requires a fresh controller decision;
- `idempotent_all_fences`: replay requires an idempotent operation and every
  declared configuration, source, event, content, command, supersession,
  deadline, and prior-commit fence.

Cancellation requests set `cancel_requested`; a later attempt or controller
transition records terminal cancellation. Supersession is a separate terminal
outcome used when newer desired work replaces an older specification.

## Queue, executor, and priority classes

Queues describe work semantics:

- `routine`: segment, TTS, audio, preview, and regeneration work;
- `maintenance`: reconciliation, repair, import, backfill, and recomputation;
- `control`: serialized controller-only mutation.

Executor authority is separate:

- `routine_worker` consumes only `routine`;
- `maintenance_worker` consumes only `maintenance`;
- `controller` consumes only `control`.

The bounded priorities are `safety_critical`, `high`, `normal`, and `low`.
Their ordered numeric values are an internal comparison contract, not an
unbounded caller-controlled field. Maintenance work cannot implicitly outrank
alert artifact deadlines. Workers never receive final broadcast publication or
configuration-commit authority.

## Initial job-type registry

Every initial type declares schemas, versions, executor, queue, priority,
timeout, deadline, retry, replay, deduplication, cancellation, capability,
fencing, command relationship, and final commit authority.

| Job type | Queue / executor | Deadline | Replay | Deduplication | Configuration fence |
|---|---|---|---|---|---|
| `routine.segment.build` | routine / routine worker | 300s default | idempotent, all fences | coalesce latest segment generation | required |
| `routine.tts.synthesize` | routine / routine worker | 180s default | idempotent, all fences | exact content | required |
| `routine.audio.convert` | routine / routine worker | 180s default | idempotent, all fences | exact conversion | required |
| `routine.cycle.regenerate` | routine / routine worker | 300s default | authoritative revalidation | coalesce latest cycle | required |
| `maintenance.reconcile` | maintenance / maintenance worker | 1800s default | authoritative revalidation | exact target | optional |
| `control.config.validate` | control / controller | 120s default | authoritative revalidation | exact candidate | required |
| `control.config.commit` | control / controller | explicit | never | coalesce latest candidate | required |
| `alert.artifact.generate` | routine / routine worker | explicit | authoritative revalidation | exact source/event/content | required |

Alert artifact generation is contract-only. The current alert dispatcher,
freshness decisions, final validation, Liquidsoap mutation, and publication
remain controller-owned. Alert jobs require an explicit deadline, one attempt,
exact source/event/content identity, and non-blind replay.

Payload and result schemas are version `1`. They use bounded identifiers and
artifact/content references. Audio, products, synthesis prose, request objects,
database handles, Python exceptions, and binary data are not embedded.
Capability requirements use a closed, bounded namespace and normalized
parameters. P1-09 maps them to pure dynamic worker qualification as documented
in [`worker-capabilities.md`](worker-capabilities.md).

## API acceptance and lifecycle

`POST /v1/cycle/rebuild` admits work to the existing supervised
refresher/conductor and therefore returns `202 Accepted`. The response contains
the stable command ID, `accepted` state, request ID, and
`/v1/commands/{command_id}` status URL. It does not claim the cycle was rebuilt.
Until the cycle-rebuild consumer and controller-finalization path are connected
to the durable repository, this transitional command remains accepted rather
than fabricating completion.

Operations that await their full declared controller mutation retain their
current synchronous status. Authentication, route policy, idempotency, RFC
9457 errors, and command inspection remain unchanged.

Both command and job-spec admission use the controller lifecycle gate. New
admission fails closed after drain starts; read-only command inspection may
continue while the API is alive. The job package does not own shutdown or
lifecycle transitions.

## Deferred implementation

The following are deliberately not implemented by the command/job contract
layer:

- P1-08 now supplies separate SWWP/1 schemas, session machines, and
  simulated-only peers under [`swwp.md`](swwp.md); it does not change these
  contracts or add a live connection;
- P1-09 supplies simulated dynamic worker capability, health, capacity, epoch,
  qualification, and reservation behavior without a live worker;
- P1-10 supplies controller-owned artifact staging, strict result fencing,
  immutable blob insertion, atomic target promotion, and durable result
  ordering for the declared artifact-producing contracts. Existing broadcast
  writers are not yet migrated.

No embedded executor is a production fallback. The P1-07 scheduler returns
typed assignments but adds no executor or worker runtime.
