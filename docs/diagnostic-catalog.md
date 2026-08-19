# Diagnostic Namespace Registry and Catalog

SeasonalWeather diagnostic definitions are immutable software content.
Canonical metadata, generated JSON, and curated explanations live under
`seasonalweather/diagnostics/catalog/`. Runtime code loads the generated
resources with `importlib.resources`. Mutable occurrences, counters, exception
evidence, and active/resolved state are not catalog content.

## Versions

Catalog version `1` is the first externally contractual code allocation.
Diagnostic schema version `1` identifies the public catalog representation.
Its definitions record introduction version `0.18.0`, the intended
first release containing the catalog; released version `0.17.0` did not
contain it.
They evolve independently:

- wording and remediation improvements do not require a new code;
- adding or retiring a code requires catalog-version review;
- an incompatible representation change requires diagnostic-schema review;
- a published code never changes meaning or gets reused.

The configuration compiler report is
`seasonalweather.configuration-report/v2`. It includes both versions and a
stable `code`. Its retained `rule_id` is non-contractual compatibility data.

## Namespace registry

Tokens are exact and case-sensitive. Aliases, punctuation variants,
abbreviations, and package-name inference are prohibited.

| Namespace | State | Scope |
|---|---|---|
| `SWCFG` | active | configuration compilation, validation, and reload |
| `SWRUN` | active | process lifecycle, supervision, readiness, and fatal runtime state |
| `SWCAP` | active | CAP, IPAWS, JSON-LD/API ingest, normalization, and lifecycle |
| `SWNWWS` | active | NWWS-OI transport, authentication, MUC membership, and ingest |
| `SWERN` | active | ERN continuous audio, FFmpeg supervision, SAME AFSK, and lifecycle |
| `SWTTS` | active | synthesis, audio validation, backend selection, and capability |
| `SWSEG` | active | segment generation, registry, freshness, and artifact semantics |
| `SWLQS` | active | Liquidsoap control, queue mutation, and final publication |
| `SWJOB` | active | command, job, lease, execution, cancellation, and result state |
| `SWWP` | active | worker protocol, sessions, capabilities, and compatibility |
| `SWDB` | active | SQLite, PostgreSQL, relational schema, outbox, archive, and migration |
| `SWOBS` | active | logging, metrics, tracing, syslog, and notification outputs |
| `SWBUILD` | active | build identity, provenance, image profile, and release compatibility |
| `SWCACHE` | reserved | implementation-neutral cache population, invalidation, expiry, and coherence |
| `SWREDIS` | reserved | Redis connectivity, keyspace, eviction, persistence, replication, and coordination |

Reserved namespaces are visible but cannot contain active or retired codes.
Activation requires a future architecture and catalog-version decision.

## Universal numbering taxonomy

A code is the exact namespace followed by a condition-class digit and a
three-digit opaque ordinal.

| Band | Primary operator-facing meaning |
|---|---|
| `0xxx` | namespace-wide, catalog, or genuinely general condition |
| `1xxx` | invalid, malformed, incomplete, or undecodable input |
| `2xxx` | unsupported, incompatible, contradictory, or policy-invalid state |
| `3xxx` | external dependency, transport, or protocol communication failure |
| `4xxx` | temporary degradation, retry exhaustion, fallback, or availability loss |
| `5xxx` | permanent failure, violated invariant, corruption, or fatal condition |
| `6xxx` | authentication, authorization, trust, credential, or security condition |
| `7xxx` | resource, capacity, quota, size, timeout, or deadline condition |
| `8xxx` | lifecycle, startup, restart, recovery, reconciliation, drain, or shutdown condition |
| `9xxx` | reserved and unassignable in catalog version 1 |

Every `x000` boundary is permanently unassignable. Ordinals `001`–`999` are
manually and monotonically allocated within one namespace/class. They do not
encode severity, component, phase, call site, exception type, or
implementation. Those properties remain independent typed metadata.

Allocation review identifies an implemented condition, selects its class from
the primary operational meaning, allocates above the recorded ceiling, adds
complete metadata and one explanation, updates a binding only where required,
and runs deterministic checks. Gaps remain unused. Retired codes become
permanent tombstones with original identity, introduction/retirement versions,
reason, and any replacement.

## Authoring and deterministic compilation

`source.json` is strict UTF-8 JSON. Standard-library JSON avoids a second YAML
parser, rejects duplicate and unknown keys, and performs no implicit scalar
coercion. Each definition records identity, class justification, title,
summary, typed severity, explicit blocking/fatal/retryable defaults, owner,
introduction version, explanation path, relationships, references, and
supported phases. `allocation_ceilings` records monotonic review state.
`tombstones` is empty in version 1 but remains a validated schema member.

Each explanation has exactly these sections: Meaning; Trigger; Correction or
recovery; Operational effect; Rationale; Alternatives or migration; Related
diagnostics. Examples must be synthetic and sanitized. Filenames and headings
must match the code. Missing, orphaned, escaping, or contradictory resources
fail validation.

Generated `catalog.json` is sorted compact UTF-8 JSON with one final newline.
It contains no time, host, user, absolute path, random value, environment data,
or Git state.

```bash
make PYTHON=.venv/bin/python diagnostics-check
make PYTHON=.venv/bin/python diagnostics-build
make PYTHON=.venv/bin/python diagnostics-export
```

Check detects drift; build atomically promotes validated bytes; export defaults
to `build/diagnostics` and accepts `DIAGNOSTICS_EXPORT_DIR`.

## Package resources and export

Package data includes `catalog/catalog.json`, `catalog/source.json`, and
`catalog/explanations/*.md`. Production lookup uses only compiled package
resources—never the working directory, `/var/lib/seasonalweather`, a mutable
override, or an operator export.

The deterministic packaging-oriented export is compatible with:

```text
/usr/share/seasonalweather/diagnostics/
    catalog.json
    explanations/
        SWCFG1001.md
        ...
```

P1-12 exports only to an explicit staging destination. It does not write host
`/usr/share`. Export rejects traversal and symlink destinations, replaces a
clean tree, removes stale files, and copies packaged bytes. A later package may
install this read-only; it never becomes runtime authority.

## CLI and public representations

```bash
seasonalweather diagnostics list
seasonalweather diagnostics list --format json
seasonalweather diagnostics list --namespaces
seasonalweather diagnostics explain SWCFG1012
seasonalweather diagnostics explain SWCFG1012 --format json
seasonalweather diagnostics export --output build/diagnostics
```

Success exits `0`; invalid, reserved-unassigned, unknown, retired, and export
failures exit `1`; argparse errors exit `2`. Commands initialize no controller,
database, source, worker, TTS, Liquidsoap, or occurrence store. Pure list,
detail, explanation, unknown, and tombstone representations are ready for
later application-service delegation. P1-22 exposes authenticated catalog
definitions through the API while keeping the catalog immutable and read-only.

## P1-11 configuration bindings

| Non-contractual `rule_id` | Stable code |
|---|---|
| `source.encoding` | `SWCFG1001` |
| `yaml.empty` | `SWCFG1002` |
| `yaml.syntax` | `SWCFG1003` |
| `yaml.multiple_documents` | `SWCFG1004` |
| `yaml.tag` | `SWCFG1005` |
| `yaml.non_string_key` | `SWCFG1006` |
| `yaml.anchor` | `SWCFG1007` |
| `yaml.alias` | `SWCFG1008` |
| `yaml.merge_key` | `SWCFG1009` |
| `yaml.root_mapping` | `SWCFG1010` |
| `yaml.scalar_construction` | `SWCFG1011` |
| `yaml.duplicate_key` | `SWCFG1012` |
| `schema.config_schema_type` | `SWCFG1013` |
| `schema.required` | `SWCFG1014` |
| `schema.type` | `SWCFG1015` |
| `schema.enum` | `SWCFG1016` |
| `schema.min_length` | `SWCFG1017` |
| `schema.max_length` | `SWCFG1018` |
| `schema.tuple_length` | `SWCFG1019` |
| `schema.unknown_field` | `SWCFG1020` |
| `schema.config_schema_unsupported` | `SWCFG2001` |
| `source.read` | `SWCFG3001` |
| `source.limit.bytes` | `SWCFG7001` |
| `source.limit.depth` | `SWCFG7002` |
| `source.limit.nodes` | `SWCFG7003` |
| `source.limit.collection` | `SWCFG7004` |
| `source.limit.scalar` | `SWCFG7005` |
| `compiler.issue_limit` | `SWCFG7006` |

### P1-14 staged-validation diagnostic bindings

These diagnostic binding identifiers are separate from stable validator-rule
identities. Validator stamps record the complete executed validator-rule set;
issues carry both identities and the stable code.

| Diagnostic binding | Stable code |
|---|---|
| `advisory.configuration` | `SWCFG0001` |
| `admission.invalid` | `SWCFG1021` |
| `semantic.invariant` | `SWCFG2002` |
| `compatibility.advisory` | `SWCFG0003` |
| `compatibility.unsupported` | `SWCFG2003` |
| `compatibility.degraded` | `SWCFG4002` |
| `validation.report_rejected` | `SWCFG2004` |
| `preflight.dependency_unavailable` | `SWCFG3002` |
| `preflight.degraded` | `SWCFG4001` |
| `preflight.timeout` | `SWCFG7007` |
| `advisory.deprecated` | `SWCFG0002` |

The compiler and validator preserve phases, paths, primary/related spans, origins, notes,
help, and redaction. Human output uses the stable code and explanation footer;
machine output includes both versions. No semantic/preflight condition was
added to the P1-11 parse/schema compiler itself.

Runtime occurrences/fatal boundaries remain P1-13. P1-14 adds canonical
semantic, compatibility, advisory, preflight, report-verification, fix, and
reusable-admission conditions described in
[`configuration-validation.md`](configuration-validation.md). Reload remains
P1-15; HTTP catalog/configuration routes P1-22; image and host packaging P2.
