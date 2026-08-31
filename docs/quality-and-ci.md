# Quality guardrails and CI parity

`make quality` is the governed quality interface and `make check` adds
compilation and the complete test suite. The quality chain validates governance,
the immutable diagnostic catalog, formatting, lint, mypy, basedpyright,
architecture ownership, dependencies, dead code, security, complexity, image
boundaries, and container security. Existing finding ceilings are ratchets:
they may be reduced when debt is removed, but they are not raised to accept a
new finding.

## Suppression guardrail

`make suppressions-check` runs
`tools.quality.suppressions_check`. It tokenizes Python comments and recognizes
inline Ruff, typing, Bandit, coverage, pylint, formatter, and import-sort
suppression directives. It compares the current tree with the explicit Git
base in `QUALITY_SUPPRESSIONS_BASE` (default `HEAD`). Existing suppressions are
accounted for as inherited debt; removals are allowed, but any net-new
suppression fails the check and must be replaced by a typed or structural fix.

The check uses source-line identity rather than line numbers, so ordinary code
movement does not create false additions. CI fetches full history and selects
the pull-request base or push predecessor explicitly. A deliberate suppression
change is therefore visible in the review and cannot pass silently through the
normal repository check interface. If a force-pushed event names a predecessor
that is no longer available, CI falls back to `HEAD^` rather than failing before
the quality checks begin.

`quality/exceptions.toml` remains the separate governed inventory for
architecture exceptions. Every exception still requires an owner, rationale,
scope, review date, and removal condition. The architecture scanner examines
the complete source tree; route-level tests do not treat the approved live
SWWP composition owner as a route-layer authority.

## Diagnostic and architecture coverage

`make diagnostics-check` compiles the canonical
`seasonalweather/diagnostics/catalog/source.json`, checks namespace state and
allocation ceilings, verifies bindings and explanations, and detects drift in
the packaged generated catalog. P2-08 uses the existing active `SWWP`
namespace and its `SWWP1001`/`SWWP2001` worker-diagnostic definitions; no new
namespace or permanent code is needed for the live session boundary. Any
future diagnostic addition must be allocated in the canonical source, bound by
the typed authority, explained, and compiled before runtime use.

`make architecture-check` runs the ownership scanner and its positive and
negative fixtures. SWARCH findings are not hidden by quality ceilings or
inline suppressions.

## CI providers

Forgejo is the canonical SeasonalWeather CI authority:

- `.forgejo/workflows/ci.yml` runs `make check` on `victus-fast` with the
  explicit suppression comparison base, then runs `make phase2-images` on the
  dedicated `victus-builder` Docker lane.
- `.forgejo/workflows/security.yml` runs the repository Gitleaks contract.
- `.forgejo/workflows/semver.yml` validates PEP 440 working versions and
  SemVer release tags.

GitHub parity is maintained in the corresponding `.github/workflows/` files.
GitHub uses `ubuntu-latest` and the equivalent hosted-runner package setup;
the repository phase gate, suppression base selection, security scan, and
SemVer guardrails remain the same. The Release workflow calls all three
workflows as reusable jobs and publishes only after all three pass. GitHub uses
its native Docker support.
Forgejo's CI-only bootstrap runs only on `victus-builder`, installs only the
Docker client, and rejects a missing runner-owned endpoint; ordinary runners
receive no Docker authority. The administrator contract is documented in
[`forgejo-runner-docker.md`](forgejo-runner-docker.md). GitHub results are
supporting parity evidence and do not replace Forgejo acceptance.
