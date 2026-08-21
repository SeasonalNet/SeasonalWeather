# P2-07 structured logging, metrics, and worker telemetry

P2-07 defines the application observability boundary. Application records are
JSON on stdout/stderr by default; `logs.runtime.color: always` remains an
interactive human-readable presentation override. Structured records include
bounded correlation fields when a runtime identity or request context exists.
Secrets, credentials, raw alert content, and raw synthesis text remain outside
metrics and worker telemetry.

## Metrics

The controller exposes public `GET /metrics` in Prometheus text format. The
application owns application, API, command/job, SWWP, capability, segment,
alert, TTS, reload, and archive measurements. Metric names and label schemas
are registered before use; unknown names, unknown labels, sensitive values,
and unbounded values fail closed.

Host CPU, memory, filesystem, kernel, network, load, and disk-I/O metrics
belong to Node Exporter. Per-container resource metrics belong to cAdvisor or
the selected container-runtime collector. Those collectors are integration
surfaces, not additional SeasonalWeather metric authorities.

## Worker telemetry

Worker-only counters and gauges travel through the typed SWWP telemetry
payload. The controller applies the versioned allowlist before recording a
sample and adds the authenticated worker and worker-instance identity as
bounded labels. Telemetry is rejected when its name, kind, labels, value, or
content is outside the allowlist. Worker telemetry does not create a worker
HTTP server or a second capability/health authority.

## Correlation and optional sinks

W3C trace context is accepted and returned at the API boundary and is carried
in task-local correlation context. The same bounded context is available for
API, command/job, SWWP, execution, result, and promotion seams as those paths
are exercised.

Optional syslog/TLS, OTLP, Alertmanager, and SNMPv3 adapters must use a
bounded nonblocking queue. A full queue drops the optional record and exposes
the drop through local metrics; destination failure is recorded without
blocking broadcast processing. Structured stdout/stderr remains the canonical
local stream. SNMP is reserved for bounded critical events and is not a
metrics replacement.

## Collector integration

Node Exporter owns host CPU, memory, filesystem, kernel, network, load, and
disk-I/O metrics. cAdvisor or the selected container-runtime collector owns
per-container CPU, memory, network, and filesystem metrics. SeasonalWeather
does not scrape or reproduce those authorities. The `logs.outputs.collectors`
configuration records the externally provisioned scrape targets so deployment
configuration can keep the controller `/metrics`, Node Exporter, and the
container-runtime collector in one Prometheus topology.

The external scrape jobs should resemble:

```yaml
- job_name: seasonalweather
  static_configs: [{targets: ["controller:9080"]}]
  metrics_path: /metrics
- job_name: node
  static_configs: [{targets: ["seasonalwx:9100"]}]
- job_name: containers
  static_configs: [{targets: ["cadvisor:8080"]}]
```

The target declarations are integration metadata, not a request for the
application to discover infrastructure or open collector connections.

## Optional output adapters

The configured adapters are disabled by default:

- `syslog_tls` sends bounded RFC 5424 records over TCP/TLS.
- `otlp` sends bounded OTLP/HTTP JSON log records with trace and diagnostic
  attributes when present.
- `alertmanager` sends only bounded `ERROR` and `CRITICAL` events to the
  Alertmanager v2 alerts endpoint.
- `snmpv3` uses the optional PySNMP v3arch USM trap transport for bounded
  critical events. Deployments keep authentication/privacy material in the
  named environment variables; raw credentials never enter YAML, logs, or
  event attributes.

Each destination runs behind `NonBlockingSink` and a bounded queue. The
canonical JSON stream remains active when a destination is invalid, slow, full,
or unavailable. Queue drops and delivery failures are represented by local
metrics and the SWOBS diagnostic catalog.

P2-07 does not replace P2-06 lifecycle ownership, P1-13 runtime diagnostic
ownership. P2-08 now supplies the live worker session and removes the
controller-local embedded executor; this document's telemetry boundary remains
unchanged.
