# Worker runtime and image profiles

P2-03 provides the worker-side process boundary for Revision 10. The command
is:

```bash
seasonalweather worker \
  --controller-url wss://seasonalweather:9080/v1/workers/connect \
  --profile routine-worker
```

The same values may be supplied through `SEASONALWEATHER_CONTROLLER_URL`,
`SEASONALWEATHER_WORKER_ID`, `SEASONALWEATHER_WORKER_INSTANCE_ID`,
`SEASONALWEATHER_WORKER_EPOCH`, `SEASONALWEATHER_WORKER_SLOTS`,
`SEASONALWEATHER_WORKER_PROFILE`, and `SEASONALWEATHER_WORKER_INPUT_ROOT`.
The live connection credential is supplied
through `--token` or `SEASONALWEATHER_WORKER_TOKEN`; that value is mounted only
as the worker service's dedicated secret.

## Profiles

| Profile | Queue | Advertised work |
| --- | --- | --- |
| `routine-worker` | `routine` | segment, standard TTS, audio conversion, cycle, and alert artifacts |
| `espeak` | `routine` | espeak-ng TTS and alert artifacts |
| `piper` | `routine` | Piper TTS and alert artifacts |
| `festival` | `routine` | Festival TTS and alert artifacts |
| `dectalk` | `routine` | DECtalk TTS and alert artifacts |
| `legacy-tts` | `routine` | legacy local TTS and alert artifacts |
| `voicetext-paul` | `routine` | VoiceText Paul TTS and alert artifacts |
| `spfy` | `routine` | `spfy` TTS and alert artifacts |
| `maintenance` | `maintenance` | maintenance reconciliation |
| `development` | `routine`, `maintenance` | all bounded routine and maintenance handler seams |

Each profile emits a complete epoch/digest capability manifest. Dependency
probes publish unavailable state and zero capacity when a required executable
is absent. TTS-capable profiles resolve controller-written opaque input
descriptors and execute local synthesis only inside the worker process; all
other unimplemented profile handlers remain fail-closed. The worker never
changes controller compatibility or authorization decisions.

P2-06 publishes a bounded local health record (by default at
`/tmp/seasonalweather-worker-health.json`, or the
`SEASONALWEATHER_WORKER_HEALTH_FILE` override) and exposes it through the
exec-friendly command `seasonalweather health worker`. The worker's startup
identity and lifecycle records are structured JSON stdout records. Its SWWP
registration and heartbeat also carry bounded readiness, lifecycle, and
new-job-admission state; the controller remains the authority for scheduling
and lease reconciliation.

## Runtime boundary

The worker initiates an outbound SWWP/1 connection using the exact
`seasonalweather.worker.v1` subprotocol. It sends registration, responds to
controller assignments, propagates cancellation, emits bounded typed results
or sanitized typed failures, sends heartbeats, and retains the session-machine
reconciliation behavior. A dropped connection is retried with bounded
exponential backoff; after a new registration the worker reports prior-session
leases and unacknowledged completions for controller-owned reconciliation.

Handlers receive only the typed assignment and a bounded cancellation/deadline
context. They do not import controller databases, API, broadcast, Liquidsoap,
NWWS, or artifact-publication authorities. The default reference handlers fail
closed when a deployment has not supplied its controller-owned input/artifact
resolver; they never fabricate a successful result from an opaque reference.

P2-08 owns the controller WebSocket endpoint, live controller/worker cutover,
and removal of the controller's transitional embedded TTS executor. P3-06
completes the local path: the controller writes only bounded opaque input
references, admits a durable SWWP job, and consumes a controller-committed
artifact receipt. A missing, unqualified, stale, or disconnected worker leaves
the required capability unavailable; the controller never falls back to local
execution. P2-06 adds process health and lifecycle reporting without adding a
worker-facing HTTP health server.
