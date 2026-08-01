# Staged Configuration Validation

SeasonalWeather validates one bounded configuration candidate through one
ordered pipeline:

```text
parse -> schema -> semantic -> compatibility
      -> deprecation -> advisory -> environmental preflight
```

Parse and schema remain owned by the source-mapped configuration compiler.
Semantic validation consumes its typed tree, exact source spans, origins, and
redaction decisions. Compatibility is pure over supplied software, schema,
protocol, job/result, diagnostic/catalog, and capability identities.
Deprecation and advisory rules are deterministic and versioned. Environmental
preflight is separate, explicitly requested, bounded, injectable, and
read-only.

If parse or schema fails, configuration-dependent stages are explicitly
skipped. A semantic or compatibility error prevents environmental probes from
running. Offline linting never performs network access, creates a database, or
starts controller services. A temporary dependency outage remains a preflight
condition and is never relabeled as YAML or schema failure.

The complete validation execution envelope is 600 seconds. This is one shared
hard limit owned by `seasonalweather.validation.limits`: preflight clamps any
supplied deadline to that envelope, typed validator-stamp construction rejects
a longer start/completion interval, and external report verification applies
the same inclusive bound. The admitted 64-probe maximum, four-worker ceiling,
and 30-second per-probe maximum require approximately 480 seconds in the worst
case; the remaining envelope covers bounded process-group cleanup and the
deterministic validation stages. Crossing the overall deadline fails closed
and does not emit an admissible report.

## Severity and policy

- `error` is invalid, contradictory, unsafe, or unsupported and always blocks.
- `warning` is valid but materially risky or degraded; blocking and
  acknowledgment are explicit policy.
- `deprecation` remains supported but has an owner and removal condition.
- `suggestion` identifies one concrete supported alternative.
- `info` describes effective, defaulted, inactive, or skipped behavior.

The shared policy evaluator computes validity, evaluated preflight readiness, warning
acknowledgment, severity/blocking counts, skipped-stage reasons, and whether
the report can be considered by a later reload decision. Hard errors cannot be
suppressed. CLI and future controller consumers use this evaluator rather than
reimplementing policy. `preflight_ready` is true only when the preflight stage
completed and has no blocking result. A skipped preflight is unevaluated, so it
is never ready or acceptable for a later reload decision merely because it has
no issues. Offline lint still succeeds on deterministic validity when
preflight was not requested.

## Candidate identity and validator reports

Every source-manifest entry contains its stable source name, exact bounded byte
length, exact-byte SHA-256, and availability. One canonical, source-name-sorted
JSON framing computes the source-manifest SHA-256 for both compiler results and
source bundles; one- and multi-source identities therefore use the same
algorithm and are independent of input order. Oversized and invalid-UTF-8
sources retain their distinct exact digests and lengths. When source bytes
cannot be read, their digest and length and the source-manifest SHA-256 are
explicitly `null`, byte availability is false, and the candidate is
nonreproducible; the stable source name remains bound by the complete candidate
identity.

Every environment binding records redacted presence. A present value requires
an externally supplied `hmac-sha256:<64 lowercase hex>` change identity for a
reproducible candidate; raw or free-form opaque text is rejected. Default and
generated origins record the selected schema’s deterministic declaration
identity. No environment value or secret-derived plain digest is serialized.
The report also carries a separate canonical complete-candidate SHA-256 over
the source manifest framing, selected schema, default/generated origin
manifest, and those redacted environment identities. Admission requires both
the independently expected source-byte SHA-256 and the independently expected
complete-candidate SHA-256.

The immutable validator stamp records software/build identity, validation
protocol, the P1-11 canonical supported and selected configuration schema,
SWWP/job/result,
diagnostic/catalog, capability-manifest identities, both candidate hashes, supplied
active generation, bounded validator-rule/probe identifiers, and UTC
start/completion times. Validator-rule identities are stable and distinct from
diagnostic catalog bindings; the stamp lists every rule actually executed,
including rules that emitted no finding. JSON is key-sorted and deterministic
under an injected test clock.

External report verification requires the independently expected
source-manifest SHA-256 (which may be `null` only when bytes are explicitly
unavailable), the independently expected complete-candidate SHA-256, and an
out-of-band SHA-256 of the complete canonical trusted report. The complete
report binding is never stored inside the admitted mapping or validator stamp.
It authenticates policy, all rule findings and their prose/fixes, probe
contracts/results, stage claims, summaries, and evidence even when coordinated
tampering remains internally self-consistent. Verification also strictly
bounds and recomputes candidate/origin/environment manifests, stamp
compatibility, rules, probe identities, counts, policy outcomes, stage
dependencies, and readiness.
Verification is admission only: it never stores a candidate, changes active
generation, applies configuration, or claims reload success.

## Semantic and capability analysis

P1-14 centralizes existing cross-field invariants for exchange token TTL
ordering, job-store enablement/path/timing, and lifecycle deadlines.
Diagnostics retain primary and related P1-11 source spans. Existing runtime
dataclass checks remain defensive compatibility delegates; they do not define
a competing report policy.

Semantic path comparisons use deterministic lexical, configuration-relative
normalization and never consult the filesystem. Defensive runtime startup also
compares the resolved job and operational database targets, preserving the
existing symlink-alias safeguard. Explicit read-only preflight adds the broader
environmental layer, including `samefile` detection for existing hard-link
aliases.

Capability analysis consumes immutable P1-09 controller-qualified snapshots.
It considers authorization, compatibility, operational state, freshness,
parameters, positive effective capacity, job acceptance, optionality,
broadcast criticality, and explicit fallback. A fully qualified `DEGRADED`
capability remains usable but produces a
nonblocking degraded advisory and is never represented as fully satisfied. It
does not probe workers, reserve capacity, lease work, alter an epoch/digest, or
certify itself. All eligible views are ranked before selection with disposition,
epoch, worker/instance/session identity, digest, and the complete normalized
record as a stable key. Caller ordering cannot change analysis, evidence, or
serialized reports.

Software compatibility follows SemVer precedence, including prerelease
identifier ordering and rejection of empty identifiers, invalid characters,
and numeric prerelease identifiers with leading zeroes.

## Environmental probes

Every probe declares a stable identifier, owner, deadline, required/optional
policy, fallback availability, cancellation safety, redaction policy, and a
serializable framework-owned specification. Production specifications admit
only bounded read-only local path metadata, executable lookup, and physical
file-separation operations. They are dispatched by a dedicated helper started
with multiprocessing `spawn`; the live asynchronous or multithreaded
controller is never forked and no private asyncio API is used. Tests inject an
executor rather than storing arbitrary callbacks in production probes.

At most four helpers run concurrently even when 64 probes are configured. Each
helper owns a process group. Timeout and cancellation terminate the group,
including descendants, and reap the helper before returning. Parent tasks are
always cancelled and gathered on cancellation.
Results distinguish available, degraded, unavailable, skipped, unsupported,
and indeterminate state. Timeout and internal probe failure remain distinct.
The framework replaces every returned summary with status-only text and ignores
callback-provided evidence. Typed local-path probes derive optional display
basenames from the configured resource, then apply final bounded secret
redaction; paths, secrets, bearer-like values, URLs, endpoint query
data, and unbounded probe text cannot cross the result boundary. Independent
deadlines ensure one hung optional probe cannot suppress other results.

The CLI accepts preflight only through an explicit option and delegates probe
construction to the public validation-owned factory. Default probes may inspect
only explicitly configured local paths. The jobs/database separation probe
first compares `Path.resolve(strict=False)` targets and, when both exist, uses
`os.path.samefile` to detect symlink and hard-link aliases. Distinct nonexistent
targets are distinct; indeterminate is reserved for genuine permission or I/O
uncertainty. No path text crosses the result boundary. These probes do not
discover or enable infrastructure.

## Machine-readable fixes and reusable admission paths

Fixes are limited to `replace`, `remove`, and `insert` on one typed path. They
carry diagnostic code, safety, applicability, source location, and old-value
or source-hash fencing. No fix is emitted for a secret or ambiguous choice,
and P1-14 never applies a fix.

The same bounded path contract represents configuration paths, JSON pointers,
job payloads, authentication administration, uploads, scheduled inserts, TTS
requests, segment entries, and import source/feature identifiers. Future
domain fixtures demonstrate representation reuse only; they do not create a
TTS backend, segment registry, import pipeline, or API route.

## CLI

```bash
seasonalweather config lint --config config/config.yaml
seasonalweather config lint --config config/config.yaml --format json
seasonalweather config lint --config config/config.yaml --preflight
```

Exit `0` means deterministic validation passed and, when requested, preflight
completed ready. Exit `1` means deterministic validation failed or requested
preflight was not ready. Argparse usage errors exit `2`. Ordinary offline
validation creates no runtime diagnostic occurrence database.

## Boundaries and extension guidance

P1-15 owns candidate persistence, active/candidate diffing, acknowledgment
workflow, reload disposition, safe points, preparation, atomic commit,
rollback, and retirement. P1-22 owns authenticated HTTP surfaces. P1-14 does
not implement either.

Add semantic rules only for accepted typed fields and retain all related
source locations. Add compatibility dimensions as explicit ranges or sets.
Add advisory rules with stable ownership and removal conditions. Add probes
only for existing dependencies through the validation-owned specification
factory; inject executors only in tests. All public conditions require canonical catalog bindings and curated
explanations. Code must remain compatible with Python 3.11.
