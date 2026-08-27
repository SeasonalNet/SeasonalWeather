# Health and readiness

SeasonalWeather exposes three separate health contracts.

## Process liveness

`GET /healthz` is unauthenticated process liveness. It always returns the
minimal stable JSON body `{"status":"alive"}` while the ASGI application can
answer.

The endpoint performs no dependency probes. An impaired NWWS, CAP/IPAWS, NWS
API, ERN, Liquidsoap, TTS, or optional integration therefore does not create a
process-restart signal.

## Operational readiness

`GET /readyz` is unauthenticated operational readiness. It returns `200` when
every currently configured required component can support normal broadcast
operation and `503` otherwise.

Readiness also requires controller lifecycle state `running`. It becomes
unready immediately when intentional drain begins.

Its public no-store report contains only stable component names, typed states,
and bounded reason identifiers.

## Detailed health

`GET /v1/health` requires the `read:health` scope under static, exchange, and
migration-only hybrid authentication. It returns the same readiness decision
with bounded timestamps, ages, durations, lifecycle state, and non-secret
component details.

## Component states and aggregation

Component state is one of:

- `healthy`
- `degraded`
- `unavailable`
- `disabled`
- `unknown`
- `not_applicable`

Readiness is a separate decision. Every required component must be `healthy`.
Optional `degraded`, `unavailable`, or `unknown` state makes the detailed
overall state degraded but does not make `/readyz` return `503`. Disabled and
not-applicable optional capabilities are neutral.

SQLite is required only when enabled. Runtime directories, the cycle
conductor, Liquidsoap control, the configured TTS path, and current command
admission are required. For the local backend, readiness additionally requires
qualified `tts.synthesis.v1` and `audio.alert_artifact.v1` worker capabilities;
there is no controller-local fallback. Exchange authentication storage is
additionally required in exchange and hybrid modes. The separate durable job
repository reports bounded schema/WAL/queue/lease/reconciliation state and
gates readiness when `jobs.required` is true.

## Current source and capability reporting

Source state is derived from supervised in-process state rather than new live
upstream requests. Enabled IPAWS or ERN integrations currently report
`unknown` because those runtimes do not yet expose a bounded state snapshot;
when disabled they report `disabled`.

Worker capability health uses bounded registry aggregates refreshed by live
SWWP sessions. Readiness gates only on explicitly required capability names;
a missing registry with required capabilities is explicitly `unavailable`.
When durable jobs are enabled and required, a controller with no active live
worker session reports SWWP as unavailable and never falls back to controller
execution. PostgreSQL and Redis remain `not_applicable`.

Reports never include configured paths, raw exceptions, credentials, tokens,
authorization headers, source payloads, or client details. Component and
detail collections are size-bounded.

The complete lifecycle and shutdown contract is documented in
[`lifecycle-shutdown.md`](lifecycle-shutdown.md).
