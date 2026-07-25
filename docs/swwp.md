# SeasonalWeather Worker Protocol (SWWP/1)

The **SeasonalWeather Worker Protocol**, pronounced “swip” (`/swɪp/`), is the
typed live-session protocol between the controller and bounded workers. Its
initial identity is:

```text
protocol: SWWP
wire version: 1
WebSocket subprotocol token: seasonalweather.worker.v1
```

P1-08 implements schemas, deterministic state machines, a durable scheduler
adapter, and in-memory simulated peers only. It does not provide a WebSocket
listener or client, worker process, credential loader, handler, or production
execution mode. Real authenticated outbound worker connections remain Phase 2
work.

## Authority

The controller remains the only authority for durable jobs, scheduling,
leasing, acknowledgment policy, cancellation, retry/deadline decisions, result
commitment, command aggregation, configuration generations, artifact
promotion, Liquidsoap mutation, and broadcast publication.

SWWP is a live transport:

- writing a `job` message is not worker acceptance;
- receiving `job_result` is not durable completion;
- `result_committed` is sent only after the P1-07 repository returns a durable
  result receipt;
- disconnect is not success, failure, or cancellation;
- worker memory cannot create or restore a missing durable job;
- simulations and worker state machines never open either controller database.

Long-running sources such as NWWS-OI remain lifecycle-supervised services and
are never SWWP jobs.

## Envelope and codec

Every message is one UTF-8 JSON object with these fields:

```text
protocol
protocol_version
message_type
message_id
sent_at
session_id
worker_id
worker_instance_id
controller_epoch
worker_epoch
payload
```

Pre-registration fields that do not yet exist are `null`. Once registered,
session and epoch identity must match every message. IDs are bounded opaque
identifiers and timestamps are timezone-aware UTC values.

The codec rejects duplicate keys, unknown envelope or payload fields, unknown
message types, invalid UTF-8/JSON, NaN and Infinity, excessive encoded bytes,
strings, maps, arrays, or nesting, and non-JSON values. Serialization uses
sorted keys and compact separators, producing deterministic canonical JSON.
Credentials, authorization material, raw exceptions, tracebacks, and
secret-shaped fields are prohibited. SWWP carries references and bounded
metadata rather than binary/base64 artifacts.

Parse failure occurs before a session machine or repository port is called.

## Messages

SWWP/1 defines these complete message families:

```text
register / registered / registration_rejected
heartbeat / heartbeat_ack
capability_update / capability_update_ack
capability_probe / capability_report
job / job_accepted / job_rejected / job_progress / job_result / job_failed
cancel / cancel_acknowledged
drain / drained
reconcile / reconcile_result
result_committed
protocol_error
```

Every payload is a strict immutable schema. Job identity always includes the
durable job, lease, attempt ID, and positive attempt number. Assignment carries
absolute deadline, lease expiry, acknowledgment deadline, job/queue/executor,
payload and result schema versions, configuration generation, typed payload,
and bounded static capability requirements.

Capability update/probe/report messages carry complete records, partial
changes/removals, validity, a full-manifest digest, and correlated full or
targeted reports. P1-09 interpretation, qualification, hysteresis, and
capacity rules are documented in
[`worker-capabilities.md`](worker-capabilities.md).

## Registration and authentication policy

The first application message is exactly one `register`. It contains:

- stable worker ID and per-start instance ID;
- positive worker epoch;
- current software and build identity;
- requested queues and slot count;
- initial capability-manifest transport data;
- supported SWWP, per-job payload, per-job result, diagnostic,
  capability-manifest, and configuration-schema versions.

Authentication happens before registration acceptance through a typed policy
interface. The authenticated transport principal declares its stable worker
identity, enablement, expiration and revocation state, and queue, job-type, and
capability allowlists. Raw credentials never enter a message or policy result.

The controller requires the principal worker ID to match registration. Accepted
queues, job types, and capabilities are deterministic intersections of the
request and static authorization. The `registered.effective_capabilities` field
contains only capabilities that are currently trusted, compatible, accepting
new work, and have positive effective capacity; it is not an echo of implemented
or authorized names. Remote sessions can never gain the `control` queue,
controller executor, or `control.*` job authority through advertising. A
rejected registration allocates no session ID.

TLS, HTTP middleware, API-token reuse, file loading, and a worker credential
database are intentionally absent.

## Independent version negotiation

The following version axes negotiate independently:

- SWWP wire version;
- payload schema version by job type;
- result schema version by job type;
- diagnostic protocol version;
- capability-manifest version;
- configuration-schema version.

Each selection is the highest supported intersection. Required axes without an
intersection reject registration. A job type is authorized only when both its
payload and result versions are selected. Selections are immutable for the
session. The exact subprotocol offer must be
`seasonalweather.worker.v1`; ambiguous or incompatible offers fail closed.

## Session state machines

Controller states are:

```text
awaiting_registration -> active -> draining -> closed
                    \-> rejected
active/draining     \-> failed
```

Worker states are:

```text
disconnected -> registering -> active -> draining
                               \-> reconciling -> active
registering/active/draining/reconciling -> closed/failed
```

Allowed messages and transitions are explicit. Terminal states are immutable.
An identical repeated message ID returns the retained response without another
repository mutation. Reusing an ID with different content is a fatal sequence
violation. Old session, controller epoch, worker instance, or worker epoch
identity cannot mutate a job.

Each accepted connection receives a new session ID. Controller and worker
restarts are represented by new controller and worker epochs, respectively.
Transport loss closes/disconnects protocol state without inventing a job
outcome.

## Assignment, heartbeat, and progress

The controller adapter first acquires a P1-07 durable lease, then constructs a
`job` message and records only session-local delivery. It calls the durable
acknowledgment/start port only after a matching `job_accepted`. A rejection is
a scheduling/reconciliation event, not handler failure. Missed acknowledgment
is left to P1-07 durable reconciliation.

Heartbeat carries bounded active lease references and the current capability
epoch/digest. A matching trusted manifest refreshes controller-time validity;
a gap or mismatch blocks new qualification and requests a full report.
Matching active leases renew through P1-07, whose
lease extension is capped by the absolute deadline. Unknown or stale leases
are returned for reconciliation and are never recreated. Heartbeat timeout
closes the session without deciding work outcomes. No production heartbeat
loop exists in P1-08.

Progress must match a current session-local durable assignment and calls the
P1-07 bounded progress port. Stale progress fails closed.

## Result, failure, and commitment

`job_result` contains current lease/attempt identity, the negotiated result
version, bounded typed result metadata, optional artifact references, and a
completion ID. It never contains a large artifact.

The controller validates and commits through P1-07. Only a returned
`ResultCommitReceipt` permits `result_committed`. Identical replay after a lost
acknowledgment is idempotent; a conflicting completion or stale lease fails
closed. The simulated worker retains completion metadata until it sees
`result_committed` or reconciliation resolves it.

`job_failed` maps to the P1-06 attempt outcome and failure category with a
bounded safe error code/summary. Raw exceptions and tracebacks are forbidden.
P1-08 performs no artifact validation, promotion, Liquidsoap mutation, or
broadcast publication.

## Cancellation and drain

Controller cancellation is recorded durably before a `cancel` message is
returned for delivery. `cancel_acknowledged` proves observation only; final job
state remains repository-owned.

`drain` prevents new session assignments and carries a bounded deadline and
reason. `drained` reports active leases and unacknowledged completions without
claiming terminal outcomes. P1-08 adds no network task or lifecycle authority.

## Reconnect and reconciliation

A reconnect registers a new session, then reports bounded prior-session work
with lease/attempt identity, acceptance and cancellation observations, and
completed-but-unacknowledged result metadata. Controller decisions are:

```text
resume
renew
cancel
resend_result
already_committed
revalidation_required
discard_stale
unknown
```

Decisions derive from the durable repository. Matching running work may resume;
matching leased work may renew; terminal or stale work is discarded; pending
cancellation is re-sent; ambiguous work requires revalidation; missing durable
work remains unknown. Worker memory never recreates it. Safety-critical replay
is never inferred merely from reconnect.

## Bounds and protocol errors

The in-code default limits are positive and bounded:

- 65,536 encoded bytes;
- 2,048 characters per general JSON string;
- 64 collection/map items;
- nesting depth 12;
- 32 heartbeat leases;
- 64 reconciliation items;
- bounded duplicate/error retention;
- heartbeat timing between declared positive limits.

Protocol-local error categories distinguish malformed JSON, invalid
envelope/payload, unknown message type, unsupported version,
unauthenticated/unauthorized, registration required, state violation, stale
session/lease, unknown job, schema mismatch, oversize, sequence/rate error, and
internal rejection. Summaries and correlation IDs are bounded and carry fatal
close intent.

These identifiers govern SWWP responses only. They do not replace Python
exceptions, operational logs, or future permanent SeasonalWeather diagnostic
codes.

## Simulation and deferred transport

Deterministic peers live under `tests/support`, use injected clocks and IDs,
and provide bounded in-memory queues with drop, duplicate, reorder, disconnect,
restart/stale-session, lost-acknowledgment, and duplicate-result controls.
Simulated workers only accept/reject assignments and emit preconstructed
progress/result/failure messages. They execute no handler and access no
database.

Architecture checks prevent general SWWP code from importing API, Uvicorn,
WebSocket libraries, broadcast, Liquidsoap, TTS, NWWS, worker handlers, SQLite,
or test simulation. Only `seasonalweather.swwp.adapter` may import the P1-07
job-store boundary. API and `control.py` cannot own or invoke simulation.

Dynamic capability qualification is simulated through the P1-09 controller
registry and scheduler boundary. Real WSS transport, file-backed bootstrap
credentials, worker processes, health/metrics, and deployment remain Phase 2
work.
