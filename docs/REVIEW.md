# Aggressive review targets

This page tracks important or high-volume machinery that deserves a focused,
scenario-driven review. These entries are not confirmed bugs. Move an item or a
specific failure to [`BUGS.md`](BUGS.md) only after establishing a concrete
input, ordering, or operational condition that produces the wrong result.

Unless noted otherwise, the implementation is shared by the Phase 4 integration
and Phase 3 runtime-fixes lines. Review shared code once, then verify the result
against both branch tips.

## Cross-source alert decisions and spoken output

Primary code:

- `seasonalweather/broadcast/nwws_runtime.py`
- `seasonalweather/broadcast/cap_runtime.py`
- `seasonalweather/broadcast/ipaws_runtime.py`
- `seasonalweather/broadcast/formatters.py`
- `seasonalweather/alerts/`
- `seasonalweather/same/targeting.py`

This machinery decides service-area inclusion, FULL versus voice-only handling,
SAME targets, tone-out gating, cross-source deduplication, VTEC lifecycle
changes, tracker state, and spoken text. It combines the highest measured
complexity with the largest on-air correctness impact.

Review with sanitized real products and cover:

- CAP, NWWS, IPAWS, and ERN versions of the same alert in every arrival order;
- mixed NEW, CAN, CON, EXT, and partial-cancellation VTEC tracks;
- unavailable location mappings versus a valid out-of-area result;
- WCN products without UGC, marine zones, and multiple CAP `info` blocks;
- duplicate replay after restart and failures before and after audio push; and
- DST, year rollover, malformed products, long products, and missing fields.

## Configuration compilation and transactional reload

Primary code:

- `seasonalweather/config.py`
- `seasonalweather/configuration/`
- `seasonalweather/configuration_reload/`
- `seasonalweather/validation/`

The compiler and reload state machine must keep the YAML schema, defaults,
environment inputs, semantic validation, restart/live classification, prepared
resources, durable generations, rollback, and secret redaction consistent.
The existing focused tests are substantial, so the next useful pass is fault
injection rather than more happy-path examples.

Review:

- cancellation or process failure at every prepare, safe-point, commit,
  rollback, and retirement boundary;
- concurrent reloads and stale candidate/report/generation input;
- resource activation followed by durable-commit failure and the inverse;
- rollback or old-resource retirement failure across restart;
- exhaustive schema-leaf coverage in reload disposition policy; and
- secrets in effective configuration, diagnostics, audit records, and errors.

## Durable jobs, artifact promotion, and restart recovery

Primary code:

- `seasonalweather/job_store/`
- `seasonalweather/jobs/`
- `seasonalweather/artifacts/`
- `seasonalweather/broadcast/segment_store.py`
- `seasonalweather/capabilities/`

This boundary coordinates leases, retries, cancellation, result identity,
publication journals, staged-file claiming, content-addressed storage, active
target replacement, and reconciliation. SQLite and filesystem changes cannot
share one transaction, so crash consistency is the key review question.

Review:

- termination after each journal, copy, validation, rename, fsync, promotion,
  and result-commit step;
- lease expiry or cancellation during artifact promotion;
- identical replay versus a changed result or stale generation;
- symlink, hardlink, sparse-file, and path-swap attempts;
- disk-full and filesystem-error behavior; and
- concurrent promotion to the same active target.

## TTS execution and artifact finalization

Primary code:

- `seasonalweather/tts/`
- `seasonalweather/worker/handlers.py`
- `seasonalweather/artifacts/`

This boundary combines untrusted text, engine-specific markup, local process
groups, remote streams, credentials, deadlines, cancellation, WAV validation,
volume rewriting, fallback/last-known-good policy, and atomic output promotion.
Direct test coverage is extensive; concentrate on cleanup and concurrency.

Review:

- subprocesses that fork, ignore termination, or fill stdout/stderr pipes;
- cancellation during stdin writes, token refresh, body streaming, and audio
  normalization;
- endless, oversized, truncated, silent, or malformed remote output;
- disk-full or path-swap failure during final replacement;
- configuration-generation changes during fallback or late completion; and
- two requests targeting the same destination.

## API, authentication, and startup ownership

Primary code:

- `seasonalweather/api/`
- `seasonalweather/auth/`
- `seasonalweather/commands/`
- `seasonalweather/application/`

These network-facing boundaries admit configuration changes, audio uploads,
manual originations, inserts, mode changes, tests, diagnostics, and worker
connections. `quality/exceptions.toml` also records that API startup still owns
database bootstrap pending centralized lifecycle ownership.

Review:

- the route/scope matrix and OpenAPI/runtime authorization parity;
- credential revocation racing an already-authorized mutation;
- multipart limits, filenames, symlinks, and malformed WAV uploads;
- idempotency-key reuse with a different request body;
- lifecycle drain beginning between authorization and mutation;
- slow clients and worker WebSocket authentication/subprotocol downgrade; and
- credential, configuration, and diagnostic redaction on every failure path.

## SAME, ERN, and external subprocess/input boundaries

Primary code:

- `seasonalweather/same/`
- `seasonalweather/broadcast/ern_gwes.py`
- `seasonalweather/broadcast/ern_relay_runtime.py`

These paths consume continuous or hostile external input and supervise decoder
subprocesses. Review truncated streams, invalid headers, partial subprocess
output, bounded memory, hangs, reconnect loops, cancellation, shutdown, and
conflicting location targeting.

## Deployment recovery policy

Primary code:

- `compose.yaml`
- `compose.staging.yaml`
- `tools/staging_interface.py`
- `docs/p3-07-staging.md`
- `docs/p3-08-production-migration.md`

Healthchecks do not restart unhealthy containers, and the base Compose graph
does not declare a `restart:` policy. This is not automatically a defect if an
external supervisor is authoritative, but the repository must establish and
test one recovery contract.

Review crash recovery, host reboot, unhealthy-but-running services, dependency
restart ordering, rollback with existing volumes, read-only filesystem
behavior, and tmpfs exhaustion. Decide explicitly whether Compose or an
external service manager owns restart policy and document that decision.
