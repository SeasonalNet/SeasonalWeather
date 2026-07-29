# Contributing to SeasonalWeather

SeasonalWeather controls alert and broadcast output. Contributions should be
small, reviewable, and operationally safe. Preserve alert correctness,
deduplication, cooldowns, service-area targeting, and on-air behavior unless a
change explicitly requires different semantics.

## Development setup

Create and activate a virtual environment, then install runtime and development
requirements:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
```

Use the repository's existing environment and dependency conventions. Do not
add a production dependency without a demonstrated need and explicit review.

## Change standards

- Keep changes narrowly scoped and avoid unrelated cleanup.
- Follow existing project patterns and checked-in architecture decisions.
- Read relevant call sites and tests before changing behavior.
- Preserve public interfaces and JSON shapes unless a compatibility change is
  deliberate and documented.
- Add or update tests when behavior changes.
- Do not weaken filtering, deduplication, cooldown, tone-out gating, test
  postponement, or CAP/ERN/IPAWS safeguards merely to make a check pass.
- Do not silently widen service-area matching.
- Treat station-feed JSON and Discord embeds as public-facing output.
- Keep secrets in the external environment file; never commit credentials,
  tokens, private keys, live authentication files, or production configuration.
- Do not modify generated files except through the repository-defined workflow.
- Review the final diff for unintended changes, debug output, local absolute
  paths, and secret material.

Repository configuration under `config/` is an example/default. Live behavior
normally comes from `/etc/seasonalweather/config.yaml`. When adding a config
key, update both `config/config.yaml` and `seasonalweather/config.py`, and
document any required live-config action.

Runtime wrapper sources live under `scripts/wrappers/`; installed copies under
`/usr/local/bin/` are not the source of truth.

## Required validation

The repository `Makefile` is the stable local and CI quality interface:

```bash
make format-check
make lint
make typecheck
make architecture-check
make dependency-check
make dead-code-check
make security-check
make complexity-check
make image-boundaries-check
make diagnostics-check
make diagnostics-build
make diagnostics-export
make quality
make test
```

Run the narrowest relevant checks while iterating. Before submitting a code
change, run `make quality` and the applicable tests. Report exact commands and
results, including anything skipped or blocked.

Forgejo CI installs `requirements-dev.txt` and runs the same `make quality`
target. Gitleaks remains a separate required security workflow because it scans
repository content for secrets rather than analyzing Python behavior.

## Quality tool selection

| Concern | Tool or control |
|---|---|
| Format and conventional lint | Ruff |
| Static typing | mypy |
| Declared and used dependencies | deptry |
| Dead-code candidates | Vulture at 80% confidence |
| Python security analysis | Bandit; Gitleaks remains separate |
| Cyclomatic complexity | Radon, C-or-worse findings |
| Ownership and import direction | `tools.quality.architecture_check` |
| Image content | Governed not-applicable assertion until images exist |

Configuration lives in `pyproject.toml` and `quality/`. Do not introduce a
second tool for the same concern unless it covers a demonstrated gap.

VSCodium/VS Code Pyright-compatible extensions use `pyrightconfig.json`. The
intentionally invalid architecture fixtures are excluded and ignored there
because they reference future packages and must produce analyzer errors by
design. Keep this exception limited to
`tests/architecture/fixtures/invalid/`; production code and valid fixtures
remain IDE-analyzed.

The reproducible v0.17.0 baseline, including current debt and package/process
ownership, is recorded in
[`docs/quality-baseline-v0.17.0.md`](docs/quality-baseline-v0.17.0.md).

## Quality ratchets and exceptions

`quality/baselines.toml` records the v0.17.0 finding counts. A target succeeds
when its tool completes normally and its count is no higher than the governed
maximum. When debt is removed, lower the maximum in the same change. Do not
raise a maximum merely to make CI green.

New and materially changed Python must pass Ruff formatting/linting and mypy at
the applicable boundary. The repository-wide ratchet isolates legacy debt; it
does not permit copying that debt into new code.

Every baseline record and architecture exception is time-bounded and must name:

- the violated rule or check;
- rationale;
- owner;
- affected scope;
- review date;
- removal or remediation condition.

`make exceptions-check` rejects incomplete, duplicated, or expired governance
records. Architecture exceptions live only in `quality/exceptions.toml`; do not
add blanket inline suppressions.

## Architecture ownership

The architecture check enforces these current and declared boundaries:

- controller modules cannot import worker-only packages;
- future worker modules cannot import controller runtime authorities;
- API modules cannot directly import database, TTS, Liquidsoap, or filesystem
  mutation authorities;
- future domain/validation packages cannot import ASGI, concrete database, or
  deployment concerns;
- scripts cannot duplicate application configuration, domain, persistence,
  broadcast, or TTS authorities;
- discarded `asyncio.create_task()` and `asyncio.ensure_future()` results are
  rejected;
- temporary embedded-worker compatibility defaults are rejected.

The valid and intentionally invalid fixtures under
`tests/architecture/fixtures/` prove that the rules pass and fail closed.
Current pre-P1-01 violations are narrow, documented exceptions rather than
silent defaults.

The checker does not impose a file-length limit or require speculative worker
packages. Cohesion, authority, dependency direction, lifecycle, and failure
domain are the controlling concerns.

Permanent operator diagnostic codes are manually allocated only through
`seasonalweather/diagnostics/catalog/source.json` and the reviewed binding
table. Every active code requires a curated explanation. Run
`diagnostics-check` after catalog edits; do not load canonical catalog content
from mutable runtime paths.

## Image-boundary activation

No Dockerfile or Compose definition exists at the v0.17.0 baseline.
`make image-boundaries-check` verifies that this remains true and that the
not-applicable declaration is unexpired. The first contribution that adds an
image must replace `quality/image-boundaries.toml` with per-image dependency
and authority assertions.
