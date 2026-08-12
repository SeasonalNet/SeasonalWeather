# SeasonalWeather

SeasonalWeather is a Python-based, internet-delivered weather/alert radio stream inspired by NOAA Weather Radio (NWR) broadcast workflows: continuous cycle audio plus interrupting alert cut-ins (SAME header bursts, 1050 Hz attention tone, spoken content, EOM).

It's designed for homelab / hobby IP-based stream use with a focus on resiliency, dedupe, and "don't accidentally target every county in the service area."

> **Not affiliated with NOAA / NWS / FEMA.**
> This is an **unofficial** hobby project. **Do not** rely on it for life safety.
> Always use official sources for real warnings (e.g., NOAA Weather Radio, weather.gov, local authorities).
> **Please note, SeasonalWeather is a project assisted by, and mostly coded by generative AI.**
> Keep this in mind if you're against certain projects coded with the use of such tools.

## Safety, scope, and acceptable use

SeasonalWeather can generate **valid SAME headers** and **1050 Hz attention tones**. That power comes with responsibility.

- **Intended use:** homelab / internal / IP-based streaming and testing.
- **Not intended for:** over-the-air broadcasting, public alert origination, or any use that could be confused with an official EAS/NWR source.
- **You are responsible** for compliance with your local laws and regulations and for preventing misuse.

## Support and warranty

- Provided **as-is**, with **no warranty** of any kind.
- Support is **best-effort** and may be limited depending on available time.

---

## What it does

### Sources / ingestion

- **NWWS-OI (XMPP)** ingest — room monitoring for alert payloads from NWS.
- **NWS API** ingest — forecasts, observations, hazardous weather outlooks, area forecast discussions.
- **NWS JSON-LD/GeoJSON ingest** — active alerts polling via `api.weather.gov/alerts/active`.
- **FEMA CAP ingest** - CAP XML formatted alerts polling via `https://apps.fema.gov/IPAWSOPEN_EAS_SERVICE/rest/eas/recent/`
- **GWES-ERN / ERN-JON** — Level 3 stream monitoring; decodes live SAME headers from a radio stream and relays qualifying events.

### Alert behavior

Full cut-ins (when enabled) follow:

1. **SAME header burst(s)** targeted to the alert's affected SAME/FIPS codes
2. **1050 Hz attention tone**
3. **TTS narration**
4. **SAME EOM**
5. Return to normal cycle

Low-severity CAP alerts can optionally trigger **voice-only interruptions** (no SAME/tone/EOM) with per-event cooldowns.

Freeze products are supported natively. `FZ.W` maps to SAME `FZW` (**Freeze Warning**), `FZ.A` maps to SAME `FZA` (**Freeze Watch**), and `FSW` (**Flash Freeze Warning**) is recognized as a native event code. The repo default keeps **Freeze Watch** voice-only and leaves **Freeze Warning** / **Flash Freeze Warning** commented out in the tone-out list so operators can opt in deliberately.

### Dedupe and safety gates

- Cross-source deduplication prevents the same alert from airing twice via NWWS then CAP.
- Ledger-based CAP tracking prevents restart spam.
- Tests (RWT/RMT) are gated behind configurable cooldowns and per-test postpone policies (`none`, `fixed_delay`, `delay_window`, `next_day`, `skip_day`, `skip_week`).
- No "global fallback" targeting — alerts that don't match the service area are dropped.

### Output

- Audio is produced for **Liquidsoap**, which feeds **Icecast**.
- Liquidsoap uses separate interrupt planes for FULL alerts, VOICE alerts, and routine cycle audio; FULL alerts have the highest priority and can preempt lower-priority VOICE updates.
- NWS spoken-alert prose is centralized in the broadcast product text helpers; CAP/JSON-LD, IPAWS, NWWS-OI, and API backfill remain transport/parser paths feeding the same NWS presentation layer.
- NWWS Short-Term Forecast (`NOW`) products are UGC-targeted and inserted into the routine cycle as expiring, non-SAME statements. Their spoken body begins after `.NOW...`; routing headers, signatures, and terminal machine-readable blocks are never narrated. A bounded api.weather.gov product-index poller backfills recent NOW products missed by NWWS-OI through the same targeting, ordering, dedupe, and expiry path.
- Interrupt alert audio render/push work runs through a priority dispatcher so FULL jobs are admitted ahead of queued VOICE jobs before Liquidsoap playout.
- While interrupt audio is active, routine cycle production is held; after the FULL/VOICE planes become idle, a guarded cycle-only reset discards paused/queued stale audio and rebuilds from station ID and the current time.
- Cycle and alert requests carry per-item Now Playing/IP-RDS metadata to Icecast through a base64-safe Liquidsoap control alias, avoiding quoted `annotate:` payloads on the line-oriented telnet socket.
- Typical mounts: `/seasonalweather.ogg`, `/seasonalweather.mp3`

### Ops

- Runs under **systemd** (units included in `systemd/`).
- HTTP control API (localhost-only by default) for status, cycle control, bounded broadcast inserts, and audio injection.
- OpenAPI 3.1 API document at `/openapi.json`; Swagger UI remains available at `/docs`.
- RFC 9457 Problem Details error responses (`application/problem+json`) with `code`, `details`, and `request_id` extensions for operator/debug use.
- Typed command and bounded-job contracts are documented in
  [`docs/command-job-contracts.md`](docs/command-job-contracts.md). A separate
  controller-owned WAL job database and non-executing durable scheduler are
  documented in
  [`docs/durable-job-repository.md`](docs/durable-job-repository.md); external
  workers are not yet implemented. SWWP/1 schemas, controller/worker state
  machines, and simulated-only protocol validation are documented in
  [`docs/swwp.md`](docs/swwp.md); there is no production socket or worker
  process yet.
- Dynamic worker capability records, epochs/digests, freshness, probes,
  qualification, and capacity reservations are implemented for deterministic
  simulated peers and documented in
  [`docs/worker-capabilities.md`](docs/worker-capabilities.md). They do not add
  a live worker or execution path.
- The versioned read-only diagnostic namespace registry, stable configuration
  codes, curated explanations, deterministic catalog build/export, and
  `seasonalweather diagnostics list|explain` commands are documented in
  [`docs/diagnostic-catalog.md`](docs/diagnostic-catalog.md). The catalog is
  packaged software content, not mutable runtime occurrence state.
- Deterministic staged configuration semantic/compatibility/advisory
  validation, opt-in bounded environmental preflight, independently bound
  candidate/report verification, and reusable admission diagnostics are documented in
  [`docs/configuration-validation.md`](docs/configuration-validation.md).
- Controller-owned runtime diagnostic occurrences, simulated SWWP diagnostic
  envelopes, fatal stderr fallback, and prior-shutdown reconciliation are
  documented in [`docs/runtime-diagnostics.md`](docs/runtime-diagnostics.md).
- Public handled-alerts feed API (`/v1/handled-alerts`) for external UI consumption, backed by SQLite.
  Persisted station-feed rows remain authoritative across restarts; startup does not synthesize degraded public records from AlertTracker state.
- Transactional configuration reload behavior and operator boundaries are documented in [docs/configuration-reload.md](docs/configuration-reload.md).

---

## Repo layout

```text
config/
  config.yaml       — repo template/example for runtime behaviour
  example.env       — secrets template (copy to /etc/seasonalweather/seasonalweather.env)
docs/               — design notes, state machine documentation, runtime wrapper notes
liquidsoap/         — Liquidsoap script(s)
scripts/            — bootstrap + helper scripts
  wrappers/         — version-controlled runtime wrapper scripts installed into /usr/local/bin
tools/              — standalone dev/debug utilities
systemd/            — systemd service unit files
seasonalweather/    — Python application
  main.py           — Orchestrator (hub, stays at root of package)
  control.py        — API → Orchestrator bridge
  config.py         — config loader
  discord_log.py    — Discord embed logger
  logging_config.py — central runtime/systemd logging policy
  liquidsoap_telnet.py
  cli/              — CLI tools (inject)
  same/             — SAME/EAS subsystem (encoder, decoder, event codes, listeners)
  alerts/           — alert lifecycle (CAP, NWWS products, VTEC, active registry)
  broadcast/        — cycle content generation (cycle, RWR, RWT/RMT, ERN, station feed)
  tts/              — speech synthesis (TTS engine, audio utils, VoiceText Paul VTML)
  api/              — HTTP control API (routes, models, auth, server entrypoint)
  nwws/             — NWWS-OI XMPP client and smoke-test
```

---

## Configuration

SeasonalWeather uses a **two-file configuration split**:

| File | Contains | Committed to repo? |
|---|---|---|
| `config.yaml` | All behaviour — service area, TTS, CAP, ERN, tests, cycle tuning, etc. | Yes (example) |
| `seasonalweather.env` | Secrets only — NWWS credentials, Icecast password, API tokens | **Never** |

### config.yaml

This is the single source of truth for all runtime behaviour. The file is well-commented and self-documenting. Top-level sections:

The repository example declares `config_schema: 1`. Configuration is parsed
with duplicate-key detection and strict structural types before startup.
Legacy files without the field resolve explicitly to schema 1. Use
`seasonalweather config lint --config PATH` for deterministic offline staged
validation, and add `--preflight` only for explicit read-only environmental
checks. See [`docs/configuration-compiler.md`](docs/configuration-compiler.md)
for YAML rules and [`docs/configuration-validation.md`](docs/configuration-validation.md)
for semantic, compatibility, advisory, stamp, policy, and probe contracts.

| Section | What it controls |
|---|---|
| `station` | Name, service area description, timezone, disclaimer |
| `stream` | Icecast host, port, mount |
| `cycle` | Broadcast reference points, SPC/forecast/obs/HWO tuning, heightened-mode policy, and alert-focus hold policy |
| `observations` | ASOS/AWOS station IDs for current conditions |
| `nwws` | NWWS-OI server, allowed WFOs, resiliency knobs |
| `now` | Short-Term Forecast routine-cycle enablement, spoken intro, fallback expiry, and API recovery polling |
| `policy` | Product types that trigger tone-out |
| `cap.voice.events` | Voice-only CAP events; repo default includes `Freeze Watch` (`FZA`) here |
| `same` | SAME encoding: enabled, sender ID, amplitude |
| `cap` | CAP polling: enabled, dryrun, poll interval, voice/full thresholds |
| `ern` | ERN/GWES stream monitoring and relay settings |
| `samedec` | Path and confidence for the `samedec` binary |
| `tests` | RWT/RMT scheduling, gating, and postpone policy |
| `zonecounty` | NWS UGC → SAME FIPS crosswalk |
| `mareas` | Marine zone → SAME crosswalk |
| `station_feed` | Public handled-alerts feed state backed by the SQLite read model |
| `api` | HTTP control API settings |
| `dedupe` | Cross-source deduplication window |
| `tts` | TTS backend selection and VoiceText Paul tuning |
| `audio` | Sample rate, tone frequencies, silence durations |
| `paths` | Working directories |
| `service_area` | Transmitter SAME/FIPS lists (the most important section) |

TTS execution uses one backend-neutral synthesis boundary. The `local` block
contains local engine, voice, and rate settings; older flat local settings are
accepted and normalized deterministically. `local` is the functional backend
in the current release. `seasonal_ttsd` and `openai_compatible` are optional
P1-17 adapters, disabled and unconfigured by default. Selecting either remote
backend requires its explicit provider configuration and credential file.
Remote output still passes through the common P1-16 finalization and
media-validation path; the current fallback policy is remote primary to
optional local fallback only.

### seasonalweather.env

Only the following belong in this file:

```bash
# Required
NWWS_JID=yourjid@nwws-oi.weather.gov
NWWS_PASSWORD=yourpassword
ICECAST_SOURCE_PASSWORD=yourpassword

# Required for the canonical static API authentication mode
# Configure exactly one of these credential sources.
SEASONAL_API_TOKEN=replace-with-a-strong-random-token
# SEASONAL_API_TOKENS_JSON='{ ... }'

# Optional — only if you changed the Liquidsoap telnet port
LIQUIDSOAP_TELNET_HOST=127.0.0.1
LIQUIDSOAP_TELNET_PORT=1234

# VoiceText Paul only — Wine subprocess environment vars
# (not read by Python; passed to the wine process by the synth wrapper)
VOICETEXT_PAUL_WINEDEBUG=-all,+seh,+tid,+timestamp
# ... etc
```

Everything else that was previously in `.env` (SEASONAL_CAP_*, SEASONAL_ERN_*, SEASONAL_TESTS_*, SEASONAL_CYCLE_*, VOICETEXT_PAUL_RETRIES, etc.) is now in `config.yaml`.

### API authentication modes

`api.auth.mode` is required in current configuration and accepts exactly
`static`, `exchange`, or `hybrid`. `static` retains the long-lived bearer-token
model for development, recovery, loopback-only administration, minimal
deployments, and break-glass access. `hybrid` is migration-only compatibility,
not a preferred steady state.

`exchange` uses controller-owned SQLite client and access-token records.
Long-lived client credentials use the strict
`swc_<public-id>.<secret>` format and are accepted only as
`Authorization: SeasonalClient ...` on `/v1/auth/token` and
`/v1/auth/revoke`. Short-lived access tokens use the disjoint
`swa_<public-id>.<secret>` format and are accepted only as Bearer credentials on
protected resources. Credentials are generated with secure randomness; only
one-way SHA-256 verifiers are stored. Raw client credentials are shown only by
client creation or rotation, and raw access tokens are shown only by successful
issuance. There are no refresh tokens.

Each client has explicit scopes, an explicit unrestricted-route selection or
segment-aware route prefixes, and one or more IPv4/IPv6 CIDRs. Disabled,
expired, revoked, or rotated clients cannot exchange or use tokens. Disablement
and rotation revoke current access tokens; enabling a client does not resurrect
them. Client revocation is terminal. Direct ASGI peer addresses are used for
CIDR checks; arbitrary forwarded-address headers are not trusted.

Access tokens default to 900 seconds. The configured minimum is 60 seconds, the
read-only maximum is 3600 seconds, and the write-capable maximum is 900 seconds.
Write capability comes from the central route/scope policy, including wildcard
behavior. Tokens never outlive their client. The controller SQLite database
configured under `database.path` stores auth state and bounded, secret-free
audit events.

Canonical static configuration uses `api.auth.mode: static`, a non-placeholder
`SEASONAL_API_TOKEN`, and one space-separated `api.auth.scopes` string. The
multi-token compatibility form uses `SEASONAL_API_TOKENS_JSON` with a JSON
array of scope strings for each token. Configure exactly one credential source.
Missing, empty, whitespace-only, malformed, placeholder, or conflicting
credentials fail closed. Existing configuration that has no `api.auth` block
may normalize to static only when exactly one valid legacy credential source is
present; the effective configuration reports that normalization. Legacy
comma-separated single-token scopes remain accepted only as an unmixed
compatibility form.

For remote API use, terminate TLS before credentials cross an untrusted
network. Credentials are accepted only in the `Authorization` header, never in
query strings, cookies, paths, or redirects. Keep a narrowly scoped static
credential for documented break-glass use where appropriate. A normal
migration is `static` → migration-only `hybrid` → `exchange`; verify exchange
clients before removing the static compatibility path.

### Authentication administration

Local client administration uses the same repository and application service
as the API:

```bash
seasonalweather auth --config /etc/seasonalweather/config.yaml client create \
  --subject automation \
  --scope read:status \
  --route-prefix /v1/status \
  --cidr 127.0.0.1/32
seasonalweather auth --config /etc/seasonalweather/config.yaml client list
seasonalweather auth --config /etc/seasonalweather/config.yaml client show CLIENT_ID
seasonalweather auth --config /etc/seasonalweather/config.yaml client rotate CLIENT_ID
seasonalweather auth --config /etc/seasonalweather/config.yaml client disable CLIENT_ID
seasonalweather auth --config /etc/seasonalweather/config.yaml client enable CLIENT_ID
seasonalweather auth --config /etc/seasonalweather/config.yaml client revoke CLIENT_ID
```

Use `--json` before `client` for deterministic machine-readable output.
`create` and `rotate` are the only commands that print a raw client credential;
capture that one-time value securely. Use `--unrestricted-routes` instead of
`--route-prefix` only when unrestricted protected-route access is deliberate.
`--expires-at` accepts an offset-aware ISO 8601 timestamp.

## HTTP API contract

The SeasonalWeather API publishes an OpenAPI 3.1 document at `/openapi.json` and interactive Swagger UI at `/docs`. The document includes the public station-feed routes and authenticated control-plane routes.

Successful JSON endpoints use `application/json`. API errors use RFC 9457 Problem Details with `application/problem+json`; callers should read the standard `type`, `title`, `status`, `detail`, and `instance` members and may also use the SeasonalWeather extensions `code`, `details`, `errors`, and `request_id`. The same request identifier is returned in the `X-Request-ID` response header.

`/openapi.json`, `/docs`, `/docs/oauth2-redirect`, `/redoc`, `/healthz`,
`/readyz`, and `/v1/handled-alerts` are public; the handled-alerts route remains
cacheable for SPA/radio clients. Other application routes require a Bearer
token and their declared scope. Mutating JSON routes also require an
`Idempotency-Key` header. Scheduled broadcast inserts live under
`/v1/inserts/*` and require the `control:inserts` scope; they add bounded,
non-SAME text or uploaded-audio segments into the normal cycle without
flushing the Liquidsoap cycle queue.

Health endpoint contracts and readiness aggregation are documented in
[`docs/health-readiness.md`](docs/health-readiness.md).
Controller lifecycle states, admission closure, task supervision, and bounded
shutdown behavior are documented in
[`docs/lifecycle-shutdown.md`](docs/lifecycle-shutdown.md).

`POST /v1/auth/token` and `POST /v1/auth/revoke` are available only in
`exchange` and `hybrid`. Both authenticate the client with
`Authorization: SeasonalClient swc_<public-id>.<secret>`. Token requests accept
only optional `scopes` and `ttl_seconds`; successful no-store responses return
`access_token`, `token_type: Bearer`, `expires_in`, and effective `scopes`.
Revocation accepts only `{"token": "swa_..."}`. Well-formed unknown, expired,
already revoked, or not-owned tokens receive the same idempotent success
response and are not enumerated.

For the wrapper install/runtime contract, see `docs/runtime-wrappers.md`.

---

## Contributing

Development setup, change standards, architecture ownership, and required
quality checks are documented in [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## Quick start

### 1) Clone to a staging path

Do **not** clone directly into `/opt/seasonalweather/app`. Clone to any temporary or staging path and run bootstrap from there. The bootstrapper rsyncs the repo into `/opt/seasonalweather/app` automatically.

```bash
git clone https://git.seasonalnet.org/SeasonalNet/SeasonalWeather /tmp/SeasonalWeather
cd /tmp/SeasonalWeather
```

### 2) Bootstrap

```bash
sudo bash scripts/00-bootstrap.sh
```

The bootstrapper is interactive by default when attached to a TTY. It tries to
use `dialog` or `whiptail` for a terminal menu, then falls back to numbered
stdout/stdin prompts with a notice if those helpers are unavailable. Automation
can keep using env vars or pass `--non-interactive`.

Install profiles:

```bash
sudo bash scripts/00-bootstrap.sh --profile standard
sudo bash scripts/00-bootstrap.sh --profile voicetext-paul
sudo bash scripts/00-bootstrap.sh --profile dectalk
sudo bash scripts/00-bootstrap.sh --profile minimal
```

Optional feature flags remain available for scripted installs:

```bash
SEASONAL_ESPEAK=1 SEASONAL_SAMEDEC=1 sudo -E bash scripts/00-bootstrap.sh --non-interactive
SEASONAL_PIPER=1 sudo -E bash scripts/00-bootstrap.sh --non-interactive
SEASONAL_FESTIVAL=1 sudo -E bash scripts/00-bootstrap.sh --non-interactive
SEASONAL_DECTALK=1 sudo -E bash scripts/00-bootstrap.sh --non-interactive
SEASONAL_VOICETEXT_PAUL=1 sudo -E bash scripts/00-bootstrap.sh --non-interactive
```

The base Python requirements no longer install the Piper/ONNX/NumPy stack.
Piper is installed only when `SEASONAL_PIPER=1` or the custom interactive
selection enables it. Festival and DECtalk build dependencies are likewise
feature-selected instead of being installed for every host.

The configuration assistant defaults to preserving an existing live config when
`/etc/seasonalweather/config.yaml` already exists. Use `--profile standard`,
`--profile voicetext-paul`, or another explicit profile only when intentionally
regenerating those high-level config choices.

The bootstrap installs the pinned Rust `samedec` decoder by default in the
standard, DECtalk, and VoiceText Paul profiles because ERN/GWES monitoring uses
it when available. To skip it on a minimal/native-decoder-only host, run:

```bash
SEASONAL_SAMEDEC=0 sudo -E bash scripts/00-bootstrap.sh --non-interactive
```

To deliberately refresh or change the pinned crate version:

```bash
SEASONAL_SAMEDEC_VERSION=0.4.2 sudo -E bash scripts/00-bootstrap.sh --non-interactive
```

VoiceText Paul is smoke-tested during bootstrap. The repo-owned wrapper retries
stateful Wine crash exits such as rc=134/139 after resetting `wineserver`; if all
attempts fail, refresh from a known-good runtime with `SEASONAL_VOICETEXT_PAUL_SOURCE`
and `SEASONAL_VOICETEXT_PAUL_REFRESH=1`.

VoiceText Paul fresh installs use a 32-bit Wine prefix
(`VOICETEXT_PAUL_WINEARCH=win32`) and install the 32-bit Wine stack. Bootstrap
also runs `loginctl enable-linger voicetext` so Wine subprocesses keep a stable
systemd user runtime instead of having logind tear the user session state down
between short-lived synth invocations. Operators may set
`VOICETEXT_PAUL_WINEARCH=auto` only as a known-good fallback.

### 3) Configure the live files

Use the configuration assistant to generate a candidate live config:

```bash
sudo seasonalweather-configure
```

The assistant writes `/etc/seasonalweather/config.yaml.new`, validates that the
application can load it, and offers to back up and apply it. It is profile-driven
and covers common operational settings; advanced operators may still edit the
live YAML directly. Generated candidate YAML does not preserve comments from the
source file.

```bash
# Behaviour — this is the LIVE config the service reads
sudo nano /etc/seasonalweather/config.yaml

# Secrets — fill in NWWS_JID, NWWS_PASSWORD, ICECAST_SOURCE_PASSWORD
sudo nano /etc/seasonalweather/seasonalweather.env
```

Do not edit `/opt/seasonalweather/app/config/config.yaml` and expect the running service to change. That file is the repo template/example only.

### 4) Start

```bash
sudo systemctl enable --now icecast2
sudo systemctl enable --now seasonalweather-liquidsoap
sudo systemctl enable --now seasonalweather
```

### 5) Check logs

```bash
journalctl -u seasonalweather -f
```

### Runtime logging policy

SeasonalWeather now has a central runtime logging policy in `seasonalweather/logging_config.py`, driven by `logs.runtime` in `config.yaml`.

The defaults are intentionally quieter for systemd/journalctl:

- suppress `httpx` and `httpcore` request lines unless you opt back in
- suppress routine CAP/IPAWS zero-change poll summaries
- suppress routine cycle conductor push chatter and segment refresher synthesis chatter
- keep normal `INFO`/`WARNING`/`ERROR` application logs for real state changes and failures

Example live config snippet:

```yaml
logs:
  runtime:
    level: INFO
    color: never        # never|auto|always; ANSI output is presentation-only
    httpx_level: WARNING
    httpcore_level: WARNING
    uvicorn_access_level: WARNING
    uvicorn_error_level: INFO
    cap_poll_summary: false
    ipaws_poll_summary: false
    conductor_cycle_push: false
    conductor_alert_push: false
    conductor_live_time_push: false
    segment_refresher_synth: false
    segment_refresher_alert_lifecycle: false
    logger_levels:
      seasonalweather.nwws: INFO
```

Set the per-logger levels back to `INFO` or enable the boolean toggles when you want the firehose during troubleshooting. Set `logs.runtime.color` to `auto` for local foreground runs, or `always` only when you deliberately want ANSI color preserved in journal output.

### 6) Listen

```text
http://<your-ip>:8000/seasonalweather.ogg
http://<your-ip>:8000/seasonalweather.mp3
```

---

## Legacy debug / test tool: inject

`inject` is a retained legacy privileged debug/test bypass. It predates the
modern API, job, capability, and artifact/origination architecture; it builds
SAME/tone/TTS audio itself, can call Liquidsoap's telnet `push_alert()`, and
may best-effort flush legacy alert/cycle queues. It intentionally bypasses the
normal modern origination lifecycle and is retained temporarily for controlled
break-glass debugging. It is not a normative SeasonalWeather control-plane
client; a later cleanup may replace, explicitly retain, or remove it.

Generate a test alert WAV and optionally push it into the Liquidsoap queue:

```bash
/opt/seasonalweather/venv/bin/python -m seasonalweather.cli.inject --help
/opt/seasonalweather/venv/bin/python -m seasonalweather.cli.inject --event DMO --loc 024021 test-alert "hello world"
```

---

## SAME encoding

SeasonalWeather uses a custom Rust `samegen` binary for fast SAME encoding for alerts it generates with SAME tones when `native_encoder` is enabled and the binary is installed and present.

The `samegen` binary is located in `tools/samegen` from the repo root, and is currently not provisioned and installed by the bootstrapper.

Configuration flags:

- `enabled:` — use `samegen` when available, otherwise, fall back to the Python encoder.
- `bin:` — What custom binary to use.
- `timeout_seconds:` — self explanatory time out for waiting for the Rust encoder to finish.
- `fallback_to_python:` — use Python native encoder if `samegen` binary is not present but enabled.

## GWES-ERN SAME decoding

SeasonalWeather uses the Rust `samedec` binary for fast SAME decoding from the ERN stream when `ern.decoder_backend` is `auto` and the configured binary exists. Fresh bootstrap installs a pinned `samedec` crate release into `/opt/seasonalweather/samedec` and publishes `/usr/local/bin/samedec`.

The `samedec` binary path, confidence threshold, and timing offset are configured in `config.yaml` under the `samedec:` section. The ERN decoder backend is configured with `ern.decoder_backend`:

- `auto` — use `samedec` when available, otherwise fall back to the native Python decoder.
- `samedec` — require the Rust decoder.
- `native` — force the pure-Python decoder.

Operational checks:

```bash
/usr/local/bin/samedec --version
/opt/seasonalweather/venv/bin/python -m seasonalweather.same.listen_samedec --help
```
