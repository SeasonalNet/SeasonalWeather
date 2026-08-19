# Runtime diagnostics and fatal boundaries

SeasonalWeather runtime diagnostics add operator-facing meaning and durable
lifecycle state to operational failures. They supplement Python exceptions;
they do not replace, flatten, suppress, or convert a fatal exception into a
clean shutdown.

## Ownership and versions

Immutable definitions, policy defaults, and explanations remain packaged in
`seasonalweather.diagnostics`. Mutable instances, occurrences, counters,
resolution evidence, process markers, and fatal handling are owned by
`seasonalweather.runtime_diagnostics`.

Runtime diagnostic, occurrence, fingerprint, worker-envelope, and occurrence
repository schemas begin at version 1. Occurrences reference diagnostic schema
and catalog versions rather than copying catalog explanations.

The sole mutable occurrence authority is a P1-13 table group in the existing
controller operational SQLite database. The runtime repository owns its
independent `diagnostic_schema_migrations` history, short parameterized
transactions, active-fingerprint uniqueness, and typed results. It uses the
operational database's busy timeout and journal policy. Simulated workers and
SWWP schemas cannot open this database.

## Instances, context, and promotion

One deeply frozen runtime instance carries the stable code and versions, trusted
catalog policy, bounded message/effect/recovery text, promotion reason,
transition intent, observed UTC time, typed correlation, and optional
exception evidence. Nested evidence and occurrence values are copied on entry,
stored as immutable mappings and tuples, and copied again into pure public
representations. Mutating caller-owned input cannot alter a diagnostic or
persisted occurrence.

Correlation is an allowlist. General identifiers are at most 128 characters,
component is at most 64, attempts are 1–1000, generations are bounded
nonnegative integers, and canonical context is at most 4 KiB. Role,
component, capability, reason code, and job class participate in the default
fingerprint. Volatile instance, session, lease, command, job, source, event,
alert, product, and segment identifiers enrich evidence but do not
automatically split storms.

Expected transient retries remain ordinary logging. An owning subsystem must
choose an explicit promotion reason. P1-13 wires optional supervised-task
degradation, required/process fatal consequence, prior-shutdown
reconciliation, and worker diagnostic compatibility/rejection. It does not
retrofit every exception path or add later subsystem diagnostics.

## Exception evidence and redaction

Evidence records exception type, bounded redacted message, notes, frame order
without locals, explicit cause, unsuppressed implicit context, and nested
exception-group members. Bounds are eight chain levels, six group levels,
sixteen members per group, 128 frames, sixteen notes, and 32 KiB total.
Truncation and cycle markers are explicit.

Capture never serializes locals, globals, closure cells, exception arguments,
arbitrary object representations, environment variables, configuration
dumps, credentials, payloads, or binary data. The original exception object
and traceback remain untouched for bare re-raise at the process boundary.

## Fingerprints and occurrence lifecycle

Fingerprint version 1 hashes canonical bounded JSON containing code,
operator-material context, exception type, and top relevant frame. It excludes
messages, raw payloads, full tracebacks, timestamps, counts, and volatile
identifiers. The repository compares canonical key material as a collision
defense. Fingerprint extraction consumes the immutable Mapping/Sequence
evidence contract directly, so recursive freezing does not remove exception
type or top-frame sensitivity.

The first promoted instance creates an active occurrence and retains full
initial evidence. Identical repeats update last-seen and a saturating count;
they retain bounded repeat transitions rather than another full traceback.
Material policy, context, effect, or recovery changes retain a full latest
instance. Explicit resolution records time, reason, evidence, and duration.
A resolved interval is immutable as resolved; recurrence creates a new
controller ID linked to the prior interval.

Active occurrences are never pruned, and creation refuses to exceed 10,000
simultaneously active records. Resolved pruning is deterministic, age/count
bounded, and limited to batches of 500. Controller startup invokes it with a
90-day age and 1,000-row retention floor. Transition history is capped at 64
rows per occurrence by discarding oldest repeat evidence first and then the
oldest remaining bounded transition when necessary. Public summary/detail
dictionaries are pure, include every version and fingerprint field plus
bounded transition and resolution evidence. P1-22 exposes bounded active and
historical occurrence summaries through authenticated API routes; it does not
make the occurrence store writable through HTTP.

Resolution evidence has a separate typed contract. Its only fields are
criterion, worker diagnostic ID, recovery state, and bounded notes. Unknown
or recursively unsafe input is rejected before SQLite access, and accepted
strings are redacted before persistence. The occurrence repository repeats
resolution-reason bounding and redaction as the final mutable persistence
authority even when its service facade is bypassed.

## Simulated SWWP diagnostics

P1-08 simulated workers can send one frozen `diagnostic` payload and receive a
`diagnostic_ack`. The payload carries schema/catalog identity, a worker-local
diagnostic ID, stable code, bounded short message and context, transition,
typed redacted evidence, and untrusted retryable/fatal hints.

The authenticated envelope supplies worker/session/epoch authority. A worker
cannot choose an occurrence ID. Resolution requires the controller-issued
relationship. Each authenticated worker relationship independently owns a
coalesced occurrence; resolving one relationship cannot resolve the durable
occurrence while another remains active. Same-version known codes use local catalog policy. Schema or
catalog mismatch creates `SWWP2001` while preserving the bounded opaque
code/message; it never imports worker catalog metadata. An unknown code under
a matching catalog or an unauthorized resolution creates `SWWP1001`. Codec
bounds, strict unknown-field rejection, session validation, and message replay
remain authoritative. This is simulated state-machine behavior only, not a
socket or worker process.

The translator retains a bounded relationship ledger keyed by authenticated
session and worker-local diagnostic ID. A repeated ID with identical content
is idempotent even in a new SWWP message envelope; contradictory reuse is
rejected. Closed-session and resolved relationships are removed within fixed
bounds. The simulated WorkerSession retains bounded acknowledgments and issued
controller occurrence relationships so it can form an authorized resolution.

Worker exception frame filenames are untrusted presentation data. Translation
retains only a redacted basename of at most 128 characters; arbitrary absolute
worker paths are never persisted. Locally captured controller frames retain
their separate bounded developer-path policy. `SWCACHE` and `SWREDIS` remain
reserved and are rejected before matching- or mismatched-catalog translation,
so the opaque compatibility path cannot carry reserved codes.

## Fatal handling and emergency output

The controller boundary covers coroutine startup, required supervised tasks,
event-loop background failures, the Uvicorn task/lifespan seam, and top-level
propagation. It attempts `SWRUN5001` persistence, performs a time-bounded
normal-log flush, and writes a deterministic redacted message directly to file
descriptor 2. Emergency rendering is capped at 16 KiB and does not require a
network sink, Discord, Liquidsoap, the database, application locks, or event
loop availability.

Reporting, persistence, flushing, rendering, and cleanup failures never replace
the primary exception. Secondary reporting failures are sanitized and bounded.
Required-task and process exceptions retain their original object, traceback,
cause/context topology, notes, and exception-group members across shutdown,
resource cleanup, and signal/event-loop handler removal. Those secondary
failures share the fatal boundary's four-entry redacted ledger.
The fatal module is isolated from database and repository/service imports,
flush exceptions are caught, and emergency output truncates on a valid UTF-8
boundary. Root and nested cause, context, and exception-group evidence render
their explicit truncation, capacity, depth, and cycle markers.
`KeyboardInterrupt`, `SystemExit`, intentional drain,
and cancellation retain existing semantics. A fatal failure is re-raised and
therefore exits nonzero; it never emits the clean stopped transition.

Exceeding the controller's total shutdown deadline, incomplete resource
cleanup, marker integration failure, or handler-removal failure is a terminal
non-clean outcome. The current marker is retained, `SWRUN5001` is reported at
the outer boundary when persistence is available, and the process exits
nonzero.

Python `faulthandler` is enabled once for the service role and the reusable
boundary. It does not replace graceful signal ownership and cannot guarantee
reporting after `SIGKILL`, kernel OOM termination, host power loss, or severe
native corruption.

## Incomplete prior shutdown

The service-role marker lives beside the configured operational database under
the existing state root. It contains only schema, role, controller instance,
advisory PID, start time, application identity, optional configuration
generation, and last lifecycle stage.

Creation and updates use a verified non-symlink state root, restrictive files,
atomic replacement, file and
directory synchronization where supported, and a nonblocking local lock.
Symlinks, unsafe modes, oversized/malformed/future markers, contradictory
pending evidence, and concurrent startup fail closed. A clean authoritative
shutdown removes only its current marker. Fatal or abrupt termination leaves
it.

The production lifecycle authority uses a controller-owned adapter to update
the marker at starting, running, draining, stopping, stopped, and failed
transitions. The adapter retains the first bounded marker failure without
throwing through a generic lifecycle transition, so readiness and shutdown
events cannot be stranded. Its controller-owned failure event initiates drain
when a RUNNING-stage marker update fails before any independent shutdown
request. A retained failure prevents later clean marker removal and becomes a
truthful terminal failure.

Clean completion is deliberately ordered: the adapter explicitly writes the
final stopped marker stage, successfully finalizes only the current marker,
and only then calls `Lifecycle.mark_stopped()` to emit `service_stopped`.
Failure at either marker-finalization step leaves the lifecycle non-clean and
exits nonzero without a stopped event.

The next start preserves prior evidence before writing its own marker.
After occurrence persistence is available, it promotes idempotent
`SWRUN8001` evidence and removes the pending copy. Persistence failure leaves
the pending evidence for another attempt. The marker never treats PID reuse as
proof of a live process or claims the exact cause of interruption.

Before pending evidence is removed, `SWRUN8001` retains only the typed,
redacted prior role, controller instance ID, advisory PID, UTC start time,
application identity, optional configuration generation, and last lifecycle
stage. Marker schema and arbitrary fields are not copied into occurrence
evidence.

## Deferred work

P1-14 owns semantic validation and preflight; P1-15 owns transactional reload;
P1-18 owns normalized NWWS adapter diagnostics; P1-22 owns authenticated HTTP
catalog and occurrence routes. Real workers, sockets, build stamps,
containers, metrics/tracing redesign, PostgreSQL, cache, and Redis remain
deferred.
