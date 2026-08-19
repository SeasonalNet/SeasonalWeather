# Phase 1 integration and exit-gate guide

This guide is the Phase 1 operator/developer index for Revision 10. It covers
the P1-23 integration boundary only. It does not authorize deployment, service
restart, live-provider access, production audio mutation, external worker
operation, container work, PostgreSQL/PostGIS work, or Redis work.

## Phase 1 boundary

Phase 1 establishes the controller contracts, deterministic handler seams,
simulated SWWP peers, source adapters, configuration and diagnostic
authorities, independent segment builders, and authenticated API surfaces.
The Phase 1 embedded executor is test scaffolding. It is not a compact
production topology and expires at the Phase 2 worker exit gate.

The controller remains the authority for configuration commits, alert
lifecycle decisions, final artifact promotion, Liquidsoap mutation, and
broadcast publication. Workers and adapters must not create parallel
authority for those decisions.

## Developer validation matrix

Run the repository quality interface with the checkout's virtual environment:

```bash
make PYTHON=./.venv/bin/python quality
./.venv/bin/python -m pytest -q
```

The complete suite is the acceptance run. Focused regressions should be used
to localize failures, not to replace the complete run.

| Boundary | Primary tests and documentation |
| --- | --- |
| Quality and ownership | `tests/test_quality_tooling.py`, `tests/test_architecture_check.py`, `docs/quality-baseline-v0.17.0.md` |
| Commands and jobs | `tests/test_command_api_acceptance.py`, `tests/test_job_repository.py`, `docs/command-job-contracts.md`, `docs/durable-job-repository.md` |
| SWWP and capabilities | `tests/test_swwp_codec.py`, `tests/test_swwp_state_machines.py`, `tests/test_capability_simulation.py`, `docs/swwp.md`, `docs/worker-capabilities.md` |
| NWWS replay and lifecycle | `tests/test_nwws_source_adapter.py`, `tests/test_nwws_product_segments.py`, `docs/nwws-source-adapter.md` |
| TTS and fallback | `tests/test_tts_boundary.py`, `tests/test_tts_async_bridge.py`, `docs/p1-16-tts.md`, `docs/runtime-wrappers.md` |
| Configuration reload | `tests/test_configuration_reload_*.py`, `docs/configuration-reload.md`, `docs/configuration-validation.md` |
| Runtime and fatal diagnostics | `tests/test_runtime_diagnostics*.py`, `docs/runtime-diagnostics.md`, `docs/diagnostic-catalog.md` |
| Segments and broadcast output | `tests/test_segment_registry.py`, `tests/test_p1_20_segments.py`, `tests/test_offnt2.py`, `docs/segment-registry.md`, `docs/p1-20-segments.md`, `docs/offnt2.md` |
| API surfaces | `tests/test_p1_22_api.py`, `tests/test_api_auth.py`, `README.md`, `docs/configuration-validation.md` |

The P1-23 decomposition regression requires `seasonalweather/control.py` to
remain a composition and compatibility facade. Direct upload persistence,
manual-origination coordination, scheduled-insert persistence, broadcast
command mutation, and runtime/configuration read-model assembly belong to the
explicit application services listed in the regression and must not be
reintroduced into the facade.

## Operator checks

Before a Phase 1 review, confirm:

1. The integration branch is at the intended signed packet head and the
   working tree is clean.
2. `make quality` passes without increasing a governed ceiling or adding an
   unowned exception.
3. The complete pytest suite passes in an ordinary shell.
4. Local-only and unconfigured optional functionality remain safe: remote TTS
   adapters stay disabled, NWWS remains controller-owned, and no external
   worker is required.
5. Representative existing broadcast products retain their established
   output; new OFFNT2 behavior is covered by representative fixtures.
6. No production service, live configuration, provider, NWWS connection,
   Liquidsoap queue, or production audio path was changed by the review.

## Representative broadcast comparison

Use sanitized, versioned fixtures and compare the rendered segment/audio-input
text at the established boundary. Record the fixture identity, configuration
identity, renderer/provenance identity, expected output hash or bounded text
comparison, and any intentional P1-21 difference. Do not use a live provider
or production state as a test fixture, and do not replace controller-owned
publication with a test helper.

## Phase 1 review record

The final P1-23 handoff must record, separately:

- requester-authoritative `make quality` and complete-suite results;
- focused integration and failure-injection results;
- representative broadcast comparison evidence;
- documentation and operator-runbook coverage;
- `control.py` lines before/after, moved responsibilities, authoritative
  owners, compatibility delegation, and deferred debt;
- quality ceilings, architecture exceptions, and their owners/removal dates;
- explicit Phase 1 result: pass, partial, or blocked.

Phase 2 must not begin until the explicit review result is recorded and every
§12 criterion is either passed or has a documented, approved disposition.
