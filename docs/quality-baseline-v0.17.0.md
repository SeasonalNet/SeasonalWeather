# SeasonalWeather v0.17.0 quality baseline

Recorded for packet P1-01 on 2026-07-23.

## Reproducible identity and environment

- Application version: `0.17.0` from `seasonalweather.__version__`
- Git revision: `a02bcb90b05aa05deee5fde77df4621512848b64`
- Git description: `v0.17.0`
- Branch at capture: `main`
- Working tree before P1-01: clean
- Baseline interpreter: repository `.venv`, CPython 3.14.4
- Host `python` command: absent
- Host `python3`: CPython 3.14.4 without pytest

Reproduce from this revision with:

```bash
python3 -m venv .venv
uv sync --frozen --group controller --group worker-runtime --group dev
make PYTHON=.venv/bin/python quality
.venv/bin/python -m pytest
```

## Pre-P1-01 automation

The repository already had:

- a Forgejo CI job that compiled Python and ran pytest;
- a separate Gitleaks 8.30.1 workflow using `.gitleaks.toml`;
- SemVer guardrails for working-tree versions and release tags;
- 169 collected pytest tests;
- runtime and alert-path smoke coverage, including Orchestrator construction.

It did not have a Makefile quality interface, formatter/linter configuration,
static typing, dependency/dead-code/complexity analysis, Bandit, architecture
ownership checks, image-boundary assertions, or governed quality exceptions.
Existing `type: ignore` comments and other suppressions predated a common
governance policy.

During P1-01 validation, 171 tests were collected after adding the two
architecture-rule tests. Per-file bounded execution observed 161 passing tests.
Two unchanged legacy test files timed out under CPython 3.14.4:

- `tests/test_api_openapi_problem_details.py` blocked on its first FastAPI
  `TestClient` request (three tests did not complete).
- `tests/test_now_products.py` passed its first two tests, then blocked in
  `test_now_runtime_queues_persistent_expiring_routine_insert` (seven tests did
  not complete).

Both files are byte-for-byte identical to revision
`a02bcb90b05aa05deee5fde77df4621512848b64`. A full-suite pass therefore could
not be established on this host; CI or a supported pre-3.14 runtime must
confirm the remaining legacy tests. No application or legacy test behavior was
changed to conceal this baseline limitation.

## Initial quality results

These counts are enforced by `quality/baselines.toml`.

| Check | v0.17.0 findings | Meaning |
|---|---:|---|
| Ruff format | 89 | Files requiring reformatting |
| Ruff lint | 992 | Selected conventional lint findings |
| mypy | 103 | Errors across 26 of 94 checked files |
| deptry | 12 | 8 unused declarations, 2 transitive imports, 2 undeclared imports |
| Vulture | 0 | Findings at 80% or greater confidence |
| Bandit | 160 | 152 low, 4 medium, 4 high |
| Radon | 182 | 124 C, 31 D, 13 E, 14 F |

Notable dependency debt is reproducible: `requests` is imported but undeclared;
`starlette` and `pydantic` are imported transitively; and deptry reports eight
declared packages as unused by static import analysis. These are recorded, not
silently removed, because runtime/bootstrap behavior requires a focused review.

The dominant Ruff debt is modernization/style work: PEP 585 and PEP 604
annotations, UTC aliases, import ordering, and simplified exception patterns.
P1-01 does not authorize applying hundreds of unrelated automatic rewrites.

Bandit results include broad exception-handling, subprocess, XML, hashing,
temporary-path, random, assertion, and SQL heuristics. Tool severity alone is
not a production-risk conclusion; each finding still requires context-aware
review before remediation or a narrow exception.

## Package and process ownership

| Area | Current ownership |
|---|---|
| `seasonalweather.main` | Controller/orchestrator construction and top-level runtime |
| `seasonalweather.api` | HTTP boundary and control-command presentation |
| `seasonalweather.control` | API-to-orchestrator application/control boundary |
| `seasonalweather.alerts` | Alert parsing, lifecycle state, VTEC, CAP/IPAWS ingest models |
| `seasonalweather.broadcast` | Cycle, alert audio, source consumers, conductor, station feed |
| `seasonalweather.database` | Controller-owned SQLite repositories and migrations |
| `seasonalweather.nwws` | Long-running controller-owned NWWS-OI client implementation |
| `seasonalweather.same` | SAME encoding, decoding, targeting, and event metadata |
| `seasonalweather.tts` | Local TTS engines and audio finalization |
| Liquidsoap process | Final queueing/mixing and stream publication |
| systemd | SeasonalWeather and Liquidsoap process supervision |
| `scripts/` | Bootstrap, preflight, and thin installed runtime wrappers |

The current dependency direction is API → control → orchestrator/runtime,
runtime → alert/broadcast/TTS/database adapters, and broadcast → Liquidsoap
control. No worker package or worker image exists. NWWS-OI remains an in-process
controller-owned source. SQLite and final Liquidsoap mutation remain
controller-owned.

Two legacy API boundaries are now explicit exceptions: the command and startup
modules directly access database authorities. Their owners, review dates, and
removal conditions are in `quality/exceptions.toml`.

## Representative behavior

The normal cycle conductor order is station ID/current time followed by data
health, station status, HWO, SPC outlook, zone synopsis, forecast, coastal
forecast, observations, and marine observations. Active alert voice segments
are inserted after the current time. Alert-focus mode keeps health, status,
HWO, SPC, and observations hot while deferring routine forecast/marine items.

A deterministic normal-mode station-status sample from the test suite is:

> And now, the station status and active alerts. SeasonalWeather is currently
> operating in normal broadcast mode. No active alerts are currently being
> tracked for the service area.

Safe local startup evidence is limited to configuration loading,
controller/Orchestrator construction, runtime-module imports, and mocked alert
paths. Full `Orchestrator.run()` waits for Liquidsoap, creates configured state
directories, clears queues, emits service/source state, and then supervises
named background tasks until the first exception. A production-like startup
and SIGTERM journal was not captured because P1-01 forbids deployment and
service restarts; the current code also has no standalone deterministic
graceful-shutdown harness. That lifecycle debt is deferred to the dedicated
lifecycle packet rather than hidden by this baseline.

## CI behavior after P1-01

Forgejo CI still compiles all Python and runs the full pytest suite. It now also
installs the pinned development tools and runs `make quality`. The existing
Gitleaks and SemVer workflows remain independent and unchanged.
