# Configuration Compiler

SeasonalWeather compiles `config.yaml` before constructing runtime services.
The compiler is the sole production authority for YAML parsing and structural
schema validation. It performs no network access, database initialization,
filesystem mutation, dependency probe, or runtime reload.

## Stage boundary

P1-11 owns:

```text
bounded UTF-8 source
  -> YAML parse and duplicate-key checks
  -> source tree and source map
  -> configuration-schema version resolution
  -> strict structural schema validation
  -> file/environment/default/generated provenance
  -> parse/schema report
```

The compiler does not decide cross-field semantics, capability or release
compatibility, environmental readiness, deprecations, reload disposition,
configuration generations, or rollback. The P1-14 staged validator consumes
the compiler result for the first four concerns without reparsing; see
[`configuration-validation.md`](configuration-validation.md). P1-15 owns
transactional reload. P1-12 binds every supported public condition to a stable
diagnostic catalog code. Compiler `rule_id` values are diagnostic binding
identifiers; P1-14 adds a separate stable validator-rule identity and records
the complete executed rule set in its validator stamp.

## Source and YAML rules

Configuration is one UTF-8 YAML document. A UTF-8 BOM is accepted and removed
before parsing, while the exact input-byte SHA-256 still includes it. Invalid
UTF-8, empty/comment-only input, multiple documents, custom tags, non-string
mapping keys, anchors, aliases, and merge keys fail closed.

PyYAML is retained as the single maintained YAML dependency. Its safe composer
provides parser-authored start and end marks; the implementation never guesses
locations with regular expressions or constructs arbitrary Python objects.
The loader uses YAML 1.2 core scalar behavior:

- only `true` and `false` resolve as booleans;
- `yes`, `no`, `on`, and `off` remain strings;
- timestamps remain strings;
- decimal, `0b`, `0o`, and `0x` integer spellings are supported;
- finite numbers are required by the schema;
- quoted and unquoted tokens retain their distinct source spans.

Duplicate keys are always invalid. The later key is the primary location and
the first definition is a related location. No last-value-wins result is
constructed.

## Resource limits

`CompilerLimits` centralizes deterministic bounds. Defaults are:

| Resource | Limit |
|---|---:|
| source bytes | 1,048,576 |
| nesting depth | 64 |
| nodes | 50,000 |
| items in one mapping or sequence | 10,000 |
| scalar Unicode code points | 262,144 |
| aliases | 0 (unsupported) |
| issues | 100 |
| related locations per issue | 8 |
| source-frame context | 2 lines |
| rendered line width | 160 code points |

Limits may be reduced through injected test policy. They are not runtime
configuration and cannot be widened by an untrusted document.

## Paths, positions, and spans

`ConfigPath` is the one internal path representation. Its segments are field
names or integer sequence indexes. JSON reports use escaped JSON Pointer;
humans see forms such as `tts.rate_wpm` and
`service_area.transmitters.KEC83[0].same_fips`.

Positions are immutable and zero-based internally. Lines, columns, and offsets
count Unicode code points in the decoded source. Spans are half-open. CLI
locations are rendered one-based. The source map retains separate key, value,
and complete-node spans, including sequence items and complete literal or
folded multiline scalars. Missing fields point at their containing mapping.
Source text remains only in the request-scoped `SourceDocument`; it is absent
from reports, location objects, and ordinary representations.

## Strict schema and version

The root `config_schema` field is a positive integer. Current and supported
schema version is `1`:

```yaml
config_schema: 1
```

Accepted legacy configuration without the field resolves explicitly to schema
1 with generated provenance. It does not mean “latest,” and the compiler never
migrates or rewrites the file. Unsupported old or future versions fail before
the document can fall through to a permissive model.

Schema 1 models every currently supported section. Unknown fields fail at each
modeled level. Required fields, strict booleans/integers/strings, finite
numbers, fixed-shape reference-point tuples, collection item types, and enums
are structural schema rules. Strings are not coerced into numbers or booleans,
and booleans are not integers. Optional and nullable fields are explicit.
Dynamic-key maps such as transmitter names, health-source names, logger
overrides, and presentation overrides have strict value schemas.

After a successful compile, the startup adapter projects the already-validated
tree into the existing frozen `AppConfig` dataclasses. It does not reparse
YAML. Existing cross-field and operational safeguards, including lifecycle,
job repository, and authentication policy checks, remain in their established
post-schema owners.

### Intentionally rejected legacy and local-only fields

Schema version 1 deliberately rejects several fields that the former
permissive loader silently ignored or that were removed with their owning
runtime:

| Rejected path | Evidence and supported behavior |
|---|---|
| `database.housekeeping.command_retention_days` | This exact field has never existed in repository configuration history. Database command cleanup has always consumed `api_command_retention_days`; use that field. |
| `database.housekeeping.asset_grace_seconds` | This exact field has never existed in repository configuration history. Audio-asset cleanup has always consumed `audio_asset_grace_seconds`; use that field. |
| `station_feed.min_write_seconds` | Removed by `fc9799069f7ffc2ef9df461a0eb1b18c905ce417` together with the legacy handled-alert JSON mirror it throttled. The station feed is now the SQLite-backed API read model. |
| `station_feed.housekeeping.keep_unparseable` | Removed by `fc9799069f7ffc2ef9df461a0eb1b18c905ce417` when station-feed ownership moved to the SQLite read model. Current housekeeping has no conditional “keep unparseable” branch. |
| `rebroadcast` | The complete section and `RebroadcastConfig` were removed by `89130c7cb3dbc071f32442697c530ddb664df4a2`. Active alert voice segments are now placed into every continuous conductor rotation by the alert tracker; the former interval, gap, TTL, item-count, and voice switches have no runtime consumer. |
| `live_time` | The complete section and `LiveTimeConfig` were removed by `89130c7cb3dbc071f32442697c530ddb664df4a2`. The continuous conductor synthesizes the time segment when its rotation reaches `time`; the former enable and interval settings have no runtime consumer. |

Before the strict compiler, unknown keys survived startup because
`load_config` selected known keys from an unrestricted YAML mapping. Their
presence therefore did not prove that they changed runtime behavior. Accepting
these names in schema version 1 would recreate that silent-ignore behavior, so
the incompatibility is intentional. Remove the obsolete paths and rename the
two database fields to their supported names.

## Origins and precedence

Provenance is independent of values and uses the same `ConfigPath`:

- `file`: exact key/value/node locations from YAML;
- `environment`: the bounded environment variable name, never its value;
- `default`: the schema or environment-default declaration identifier, with no
  fabricated source span;
- `generated`: a bounded generator identifier, with no fabricated source span.

File values provide YAML behavior. The existing named environment bindings
provide secrets, Discord webhook URLs, and Liquidsoap topology. Schema defaults
apply only when a field is absent. Generated provenance identifies the legacy
schema resolution and current derived runtime values such as the service-area
SAME/FIPS union and NWWS credential-default state.

## Secret handling

Known environment credential bindings are never serialized. Schema/path secret
classification is supplemented by conservative pre-schema matching for
passwords, tokens, credentials, authorization material, private keys, secrets,
and webhooks. Unknown secret-like fields are redacted even when schema
validation rejects them. Multiline secret bodies and surrounding parser frames
are replaced synthetically before display.

Reports, messages, locations, origins, representations, and exceptions never
contain secret values, hashes, prefixes, suffixes, or environment values.

## Issues and reports

Compiler issues contain a stable `SWCFG` code, a bounded non-contractual
`rule_id`, `parse` or `schema` phase, error severity, blocking state,
value-free message, optional canonical path, primary
location, ordered related locations, notes, help, safe origin metadata, and a
redaction marker. Parser and validator exception text is not part of the
contract. Unexpected implementation failures retain native exceptions and
tracebacks rather than becoming false configuration issues.

Issue ordering is deterministic by phase, source, position, path, rule, and
stable message tie-breaker. Compiler report version 2 includes:

- aggregate parse/schema validity;
- explicit and resolved configuration-schema versions;
- source display identifiers, optional exact-source SHA-256, and exact bounded
  byte lengths when bytes were available;
- structural issues and locations;
- safe origin counts;
- whether redaction occurred.
- diagnostic catalog and diagnostic-schema versions.

It contains no timestamp, UUID, process ID, source text, effective
configuration, environment value, or ANSI control sequence. Identical input
and environment presence produce byte-identical compact JSON.

Human errors render `error[SWCFG....]` and a deterministic
`seasonalweather diagnostics explain CODE` footer. See
[`diagnostic-catalog.md`](diagnostic-catalog.md) for the complete mapping,
versioning, and authoring rules.

## Offline lint CLI

Run deterministic staged validation without starting any operational
subsystem:

```bash
seasonalweather config lint \
  --config /etc/seasonalweather/config.yaml \
  --format human

seasonalweather config lint \
  --config /etc/seasonalweather/config.yaml \
  --format json
```

Human mode writes errors to stderr and a bounded success line to stdout. JSON
mode writes exactly one JSON document to stdout. Exit codes are:

- `0`: deterministic stages valid and requested preflight ready;
- `1`: deterministic validation invalid or requested preflight not ready;
- `2`: argparse usage error.

Lint continues through deterministic semantic, compatibility, deprecation,
and advisory stages. Environmental checks run only with explicit
`--preflight`; reload applicability and live configuration changes remain out
of scope.
