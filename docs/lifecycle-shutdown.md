# Controller lifecycle and graceful shutdown

SeasonalWeather has one controller-owned lifecycle authority shared by the API,
orchestrator, health service, task supervisor, and current admission gates.
Health routes observe this authority but cannot change it.

## States and transitions

The lifecycle states are:

- `starting`
- `running`
- `draining`
- `stopping`
- `stopped`
- `failed`

Normal startup moves from `starting` to `running`. An intentional shutdown may
move from either startup or running into `draining`, then `stopping`, then
`stopped`. An unexpected required-task failure moves the current non-terminal
state directly to terminal `failed`.

Invalid transitions fail closed. `stopped` and `failed` are terminal.
Repeated shutdown requests share the existing drain. A second `SIGTERM` or
`SIGINT` sets the explicit force flag: remaining active-request or publication
grace is shortened and bounded task cancellation begins. It does not turn a
fatal outcome into a clean stop.

## Signal and Uvicorn ownership

The controller entrypoint owns conversion of `SIGTERM` and `SIGINT` into a
lifecycle shutdown request. Uvicorn signal installation is disabled.

After admission closes, the controller sets Uvicorn's supported
`should_exit` flag and gives active requests no more than
`lifecycle.active_request_seconds`. There is no remote shutdown endpoint.
Subsystems do not call `sys.exit`.

## Ordered shutdown

An intentional shutdown proceeds in this order:

1. transition atomically to `draining`;
2. make readiness false and close mutable admission;
3. request Uvicorn active-request drain;
4. stop new routine, source, alert, TTS, and future job/lease admission;
5. wait for an already-entered alert publication section;
6. invoke registered source/resource stop callbacks;
7. cancel and await remaining registered tasks within the task bound;
8. close the NWS HTTP client and checkpoint controller SQLite state;
9. transition through `stopping` to `stopped`.

The complete cleanup is capped by `lifecycle.total_seconds`. Stage failures are
logged with bounded resource or task names and do not make shutdown wait
forever.

## Supervision

Every controller-started long-running production task has a stable name and is
registered with the controller supervisor. The registry records whether the
task is required or optional and any supported stop callback and timeout.

The API server, orchestrator, conductor, segment refresher, and alert-audio
dispatcher are required. Configured source pollers/consumers, health state,
NOW/PNS backfill, ERN, scheduled tests, housekeeping, and Discord delivery are
optional under current degraded-source policy.

Unexpected required-task completion or failure is fatal. The original
exception object, traceback, cause chain, and any `ExceptionGroup` members are
preserved for the top-level process boundary. Optional task failure is recorded
as degraded supervisor state without restarting the task during the same
process. Expected cancellation during drain is not fatal. The supervisor never
cancels arbitrary event-loop tasks that it does not own.

## Admission and publication

Admission is open only in `running`.

- Mutable API requests and transient command creation receive an RFC 9457
  `503 service_draining` response after drain begins.
- Read-only routes remain available while Uvicorn can answer.
- Routine cycle/refill creation stops.
- New alert-audio and TTS work is rejected.
- Source tasks cannot be started or reconnected. NWWS receives a permanent
  shutdown request before cancellation, so its normal reconnect loop cannot
  create another XMPP worker.
- Durable job admission and the typed `job_lease` admission class close at
  drain. The controller-owned scheduler stops returning assignments, active
  leases are reconciled within the configured shutdown bound, and the separate
  job database is checkpointed.

The alert Liquidsoap push is the current atomic publication boundary. A push
that entered before drain may finish within `publication_seconds`. New entry
after drain is rejected, so Liquidsoap mutation is either performed inside the
section or not started.

Routine segment audio already uses unique temporary files and `os.replace`.
Admission is checked again immediately before promotion. If synthesis finishes
after drain, the temporary files are removed and the previous authoritative
WAV remains in place.

## TTS and sources

TTS admission closes at drain start. Already-running synchronous local
synthesis cannot be preempted safely by every supported backend; it may finish
within the overall bound. Routine output is fenced before atomic promotion.
Alert output may proceed only if it can enter the publication boundary before
drain, otherwise it fails conservatively without a Liquidsoap push.

CAP and IPAWS pollers close their HTTP clients during cancellation. ERN kills
and awaits its decoder subprocess. NWWS permanently closes reconnect admission,
requests XMPP/thread stop, and is then awaited or cancelled within the source
and overall bounds.

## Health behavior

`GET /healthz` remains minimal process liveness and continues to return
`{"status":"alive"}` while ASGI can answer.

`GET /readyz` becomes `503` as soon as lifecycle leaves `running`.
`GET /v1/health` includes the bounded `lifecycle_state` field and lifecycle
component. No signal number, configuration path, exception text, traceback,
credential, source payload, or environment value is exposed.

## Clean and fatal outcomes

Only an intentional drain that completes controller cleanup emits
`service_stopped` and returns normally. Unexpected required-task failure sets
`failed`, performs the same bounded best-effort cleanup, re-raises the original
failure, and leaves the entrypoint nonzero. Fatal termination never transitions
through or emits the clean stopped outcome.

Lifecycle logs use stable event names and bounded state/task/resource fields.
Runtime occurrences, emergency diagnostic rendering, fatal propagation, and
previous-run reconciliation are documented in
[`runtime-diagnostics.md`](runtime-diagnostics.md).

## Configuration

All values are seconds and are restart-time configuration:

```yaml
lifecycle:
  total_seconds: 30.0
  active_request_seconds: 10.0
  publication_seconds: 8.0
  source_stop_seconds: 8.0
  tts_stop_seconds: 8.0
  task_cancel_seconds: 5.0
  resource_close_seconds: 5.0
```

Every value must be positive. `total_seconds` must be at least the largest
individual stage value. The total is a cap across the ordered sequence, not a
sum multiplied by the number of components.

## Current limits and future integration

SeasonalWeather has a typed durable job repository and lease state plus
simulated-only SWWP/1 session machines. There is still no production SWWP
network task or remote worker to drain. The controller state machine exposes a
narrow deterministic drain port for tests: it closes new session assignment,
sends bounded drain intent, marks P1-09 capability qualification
unschedulable, clears pending capacity reservations, and treats the worker
response as observation rather than durable completion. Existing leases remain
under P1-07 reconciliation authority. Later real worker, diagnostics,
TTS-adapter, artifact-fencing, and normalized NWWS-source packets must
integrate through these lifecycle and supervision boundaries without moving
lifecycle authority into those subsystems.
