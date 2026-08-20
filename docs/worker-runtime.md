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
`SEASONALWEATHER_WORKER_EPOCH`, `SEASONALWEATHER_WORKER_SLOTS`, and
`SEASONALWEATHER_WORKER_PROFILE`.

## Profiles

| Profile | Queue | Advertised work |
| --- | --- | --- |
| `routine-worker` | `routine` | segment, standard TTS, audio conversion, cycle, and alert artifacts |
| `piper` | `routine` | Piper TTS and alert artifacts |
| `legacy-tts` | `routine` | legacy local TTS and alert artifacts |
| `maintenance` | `maintenance` | maintenance reconciliation |
| `development` | `routine`, `maintenance` | all bounded routine and maintenance handler seams |

Each profile emits a complete epoch/digest capability manifest. Dependency
probes publish unavailable state and zero capacity when a required executable
is absent. The default reference handlers are intentionally fail-closed, so a
profile also publishes `implemented=false` and zero capacity until a real
deployment handler factory is supplied. The worker never changes controller
compatibility or authorization decisions.

## Runtime boundary

The worker initiates an outbound SWWP/1 connection using the exact
`seasonalweather.worker.v1` subprotocol. It sends registration, responds to
controller assignments, propagates cancellation, emits bounded typed results
or sanitized typed failures, sends heartbeats, and retains the existing
session-machine reconciliation behavior.

Handlers receive only the typed assignment and a bounded cancellation/deadline
context. They do not import controller databases, API, broadcast, Liquidsoap,
NWWS, or artifact-publication authorities. The default reference handlers fail
closed when a deployment has not supplied its controller-owned input/artifact
resolver; they never fabricate a successful result from an opaque reference.

P2-03 does not implement the controller WebSocket endpoint, complete live
controller/worker cutover, health servers, container hardening, mount/network
parameterization, or removal of the transitional embedded executor. Those
boundaries remain owned by P2-04 through P2-08 as specified by Revision 10.
