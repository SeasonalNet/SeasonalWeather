# P2-05 container security and secret isolation

P2-05 defines the security contract for the controller image and every worker
profile. The contract is recorded in
`quality/container-security.toml` and checked by
`make container-security-check` (also included by `make quality`).

Each image is built to run as the fixed non-root `seasonalweather` user (UID
and GID 10001). Deployment runtime configuration must apply the contract:

```yaml
read_only: true
cap_drop:
  - ALL
security_opt:
  - no-new-privileges:true
user: seasonalweather
tmpfs:
  - /tmp:rw,nosuid,nodev,noexec
  - /run:rw,nosuid,nodev,noexec
```

The controller may write only its operational state, job state, artifacts, and
log mounts. Workers may write only the shared artifact staging mount; they
must not receive controller operational state, the job database, or the log
mount. Configuration and the diagnostic export are read-only. Per-service
secret mounts are read-only and their files use mode `0400`; each contains
only the credentials that service requires.

The controller consumes known secret files named after their existing
environment bindings, for example `/run/secrets/ICECAST_SOURCE_PASSWORD` and
`/run/secrets/SEASONAL_API_TOKEN`. Present mounted files override environment
compatibility values and are validated as regular, non-symlink UTF-8 files with
mode `0400` and a bounded size. Missing files preserve host/systemd
environment-file compatibility. Unknown files are ignored by the application;
the service-level mount policy remains responsible for not mounting them.

The controller allowlist is explicit: NWWS, Icecast, API, and Discord bindings
may be mounted by name. Worker profiles have an empty secret allowlist because
the current SWWP worker contract does not require worker credentials. This
allowlist must be expanded together with an authenticated worker protocol, not
by mounting the controller secret directory into workers.

Application log handlers redact values following `password=`, `secret=`,
`token=`, `api_key=`, `authorization=`, or `webhook=` before output. Diagnostic
and fatal-boundary redaction remains in force as an additional boundary.

The image build context excludes environment files, private-key material, and
SQLite databases. Dockerfiles contain no secret-shaped build arguments or
environment variables, and the profile dependency locks retain the controller/
worker separation established by P2-02 and P2-03.

P2-05 does not add a Compose topology, health/lifecycle behavior, live
deployment, or worker cutover. Compose application of these mounts and runtime
flags belongs to the later deployment packets.
