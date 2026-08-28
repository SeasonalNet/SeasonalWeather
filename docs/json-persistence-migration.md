# Durable JSON persistence migration

SeasonalWeather's restart-surviving operational state is controller-owned
SQLite state. The following former JSON authorities now use SQLite tables:

| Subsystem | SQLite authority |
| --- | --- |
| NWS CAP and IPAWS deduplication | `cap_seen_ledger` |
| RWT/RMT schedule state | `scheduler_state` |
| Observation pressure trend history | `observation_pressure_history` |
| Uploaded-audio metadata | `audio_assets` |
| Controller lifecycle crash evidence | `runtime_process_markers` |
| Configuration candidate metadata and validation reports | `configuration_candidates`, `configuration_candidate_reports` |
| Cycle segment metadata and commit witnesses | `cycle_segments`, `segment_commit_journals`, `segment_commit_receipts` |

Database-enabled runtime paths do not write JSON indexes, sidecars, markers,
journals, or receipts. Existing file-backed segment receipts and candidate
metadata are accepted only as bounded one-start migration inputs when a
database record is absent; subsequent reads and writes use SQLite. The
legacy files are never recreated by a database-enabled runtime. The
database-disabled constructor mode is retained only for isolated legacy/test
compatibility and is not a supported restart-safe deployment mode.

JSON remains valid for protocol and interchange boundaries (SWWP, worker job
descriptors, SAME decoder JSONL, API/network payloads), structured logs,
transient worker health probes, and immutable packaged build/diagnostic
catalog artifacts. Those files are not restart-surviving controller state and
must not be treated as a persistence authority.

The controller operational database is therefore required for restart-safe
deduplication, scheduling, lifecycle reconciliation, reload evidence, audio
asset lookup, and segment publication. Disabling the database intentionally
removes those durable guarantees; it must not introduce a JSON fallback.

## Architecture guard

`SWARCH057` enforces this boundary in `make quality`. It rejects JSON-looking
filesystem writes and also rejects unapproved functions that combine JSON
serialization with filesystem mutation. The rule follows path aliases,
atomic-replace destinations, and the known legacy journal path factories, so a
new sidecar or renamed JSON writer cannot bypass the check by hiding the
filename behind a local variable.

The allowlist is callable-level and intentionally small. It covers only
transient health probes, worker interchange descriptors, immutable build and
diagnostic exports, and the bounded one-start legacy compatibility writers
listed in `quality/architecture.toml`. It does not authorize a new durable
state writer or a new exception for one.
