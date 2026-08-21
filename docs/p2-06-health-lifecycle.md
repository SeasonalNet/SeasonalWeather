# P2-06 container health and lifecycle records

P2-06 makes process liveness, operational readiness, startup identity, and
shutdown state explicit for the controller and capability-specific workers.

## Controller

The controller image uses:

```text
seasonalweather health controller --mode readiness
```

The command checks `/readyz`, so a responding web listener is not sufficient
for a healthy container. `/healthz` remains the minimal process-liveness
surface. `/readyz` is false until broadcast-critical startup completes and
continues to account for required Liquidsoap, TTS, storage, admission, and
worker-capability checks.

## Worker

Workers do not expose a controller-facing health HTTP server. They atomically
write a bounded health record and use:

```text
seasonalweather health worker --mode liveness
seasonalweather health worker --mode readiness
```

The default record is `/tmp/seasonalweather-worker-health.json`; deployments
may set `SEASONALWEATHER_WORKER_HEALTH_FILE`. Liveness accepts a fresh process
record while readiness requires registration and a ready worker state.
Stopped, failed, malformed, and stale records fail closed.

Worker registration and heartbeat frames carry lifecycle state, readiness, and
new-job admission. These fields are observations only. Controller
authorization, capability qualification, durable leases, and reconciliation
remain authoritative.

## Startup and shutdown records

Both roles emit one structured `startup_identity` record before subsystem
initialization. It includes software version, source revision, build ID,
image profile, runtime role, Python/platform information, and supported schema
versions without environment dumps, secrets, or raw configuration.

Controller records include configuration validation, storage, control-plane,
broadcast-path, source startup, ready/degraded, background-warmup, draining,
and stopped events. Readiness is not declared from Uvicorn startup. An
intentional `SIGTERM` closes admission, drains owned work, and produces a
clean stopped event only after bounded cleanup. Unexpected required-task
failure does not emit a clean stopped event.

P2-06 does not add Compose topology, deployment operations, production
controller/worker observability beyond the bounded lifecycle records. P2-08
owns the live SWWP cutover and removal of the controller-local executor.
