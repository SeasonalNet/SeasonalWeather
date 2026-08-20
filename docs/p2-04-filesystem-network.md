# P2-04 filesystem and network parameterization

P2-04 makes deployment paths and endpoint policy explicit without changing
which component owns durable state. The repository configuration example is
the portable template; a live installation continues to read its mounted
`/etc/seasonalweather/config.yaml`.

## Filesystem authority

The `paths` block declares the operational state, controller job state, audio
artifact root, diagnostic export, temporary, runtime, and secret mount roots.
The legacy `work_dir`, `audio_dir`, `cache_dir`, `config_dir`, and `log_dir`
fields remain accepted. When a legacy configuration omits the new fields, the
loader derives operational state, job state, and artifact roots beneath
`work_dir`.

The default layout is:

```text
/etc/seasonalweather/config.yaml        configuration, read-only mount
/run/secrets                           per-service secret mounts
/var/lib/seasonalweather/state         controller operational state and SQLite
/var/lib/seasonalweather/jobs          controller-owned job SQLite
/var/lib/seasonalweather/artifacts     staged, active, and worker artifacts
/usr/share/seasonalweather/diagnostics packaged diagnostic export
/tmp                                   temporary storage
/run/seasonalweather                   process runtime state
```

Operational and job SQLite remain separate local-filesystem databases. Worker
images do not receive either database mount; worker artifact exchange remains
bounded by the controller-owned artifact service and SWWP result references.
The diagnostic catalog is loaded from package resources. Its `/usr/share`
export is inspection content, while mutable diagnostic occurrences remain in
controller-owned operational state.

## Network authority

The `network` block declares controller API binding, Liquidsoap control
connection, future SWWP listener/path and worker-controller URL, and disabled
by-default PostgreSQL and Redis endpoint policy. Icecast host/port remain in
the existing `stream` block, and remote TTS endpoint and TLS policy remain
nested under their selected backend. Environment variables may still override
the legacy Liquidsoap host and port for host deployments.

API command-line `--host` and `--port` values are explicit overrides. When
omitted, the server uses `network.api`. The systemd unit therefore no longer
embeds a loopback bind. The worker command continues to require its outbound
controller URL through `--controller-url` or
`SEASONALWEATHER_CONTROLLER_URL`; it never discovers a controller endpoint.

Network and path changes are restart-required configuration changes. P2-04 does
not activate PostgreSQL or Redis, create the live SWWP controller endpoint,
apply container hardening, or introduce a Compose topology.
