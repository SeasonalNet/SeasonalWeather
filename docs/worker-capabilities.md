# Dynamic worker capabilities

P1-09 implements dynamic capability interpretation and qualification for
SWWP/1 workers. It remains controller operational state only: the worker
reports truthful local facts, while authorization, compatibility, qualification,
capacity, leasing, and job state stay controller-owned. P2-03 consumes these
contracts from real worker processes but does not add a capability database.

## Authority and record model

Workers report truthful local facts. The controller owns authorization,
compatibility, qualification, capacity reservation, durable leasing, and job
state.

A capability record keeps these concerns separate:

- `implemented`: whether this worker build contains the capability;
- `compatibility`: `compatible`, `incompatible`, or `unknown`, computed by the
  controller rather than supplied by the worker;
- `operational_state`: `healthy`, `degraded`, `unavailable`, `draining`,
  `disabled`, or `unknown`;
- `accepting_new_jobs`: an explicit admission decision;
- total and currently reported available capacity;
- bounded job restrictions, operating parameters, validity, timestamps, and
  dependency-health states.

Unimplemented capabilities cannot report capacity. Unavailable, draining,
disabled, and unknown states cannot accept new work. Degraded state may accept
work only when it explicitly reports admission and positive capacity. Healthy
state never bypasses authorization or schema compatibility.

## Bounded parameters

Parameters are deterministic JSON scalars or bounded homogeneous collections.
The initial allowlist covers formats, media types, voices, profiles, schema
versions, sample rates, channels, input/output limits, feature extensions, and
job-class restrictions. Unknown names, nested JSON, non-finite or excessive
numbers, paths, URLs, credentials, regexes, and Python objects fail closed.

P1-06 scalar requirements use exact matching. A scalar requirement can also be
satisfied by membership in a reported homogeneous collection. Missing or
unknown parameters never qualify a job.

## Manifests, epochs, and digests

One positive epoch sequence belongs to each worker instance. Registration
carries a complete manifest. Each proactive update or probe report advances
the worker's published epoch.

The digest format is:

```text
sha256:<64 lowercase hexadecimal characters>
```

It covers schema version and the normalized complete worker report. It excludes
controller-computed compatibility, the digest itself, message/session IDs,
receive time, credentials, and map insertion order.

For a current manifest:

- the same epoch and digest is idempotent;
- the same epoch with another digest is a conflict;
- an older epoch is stale;
- a non-consecutive update is a gap.

A partial update is applied to a copy. Changed and removed records are
normalized and the resulting complete digest is recomputed before atomic
replacement. Gap or mismatch never partially applies and requires a full
report.

## Freshness and heartbeat policy

Controller receive time owns freshness. Each worker validity is capped by the
controller maximum. Expiry deterministically changes affected records to
`unknown`, makes effective capacity zero, and blocks new assignments. It does
not fail an existing durable lease.

A heartbeat refreshes validity only when it matches an already present,
trusted epoch and digest. It cannot recreate a missing manifest. Older
heartbeats do not refresh; conflicts and unseen higher epochs require a full
report. Heartbeats do not change P1-07 deadlines.

## Hysteresis

Worker-side hysteresis is deterministic and clock-injected. Its bounded policy
contains failure and recovery thresholds, degraded/unavailable dwell, and
optional publication debounce.

Transient observations can remain below the failure threshold. Hard failures,
draining, disabled state, admission closure, and capacity reductions publish
promptly. Recovery requires the configured successes and dwell. `unknown`
remains primarily a controller freshness state. Hysteresis never determines
controller compatibility.

## Probes

Full and targeted probes carry a bounded ID, mode, targets, reason, request
time, deadline, and expected session/instance. Reasons cover registration
policy, reconnect, periodic qualification, epoch gaps, digest mismatch,
staleness, rejection races, and internal requests.

Only a current, correlated response can mutate state. Full reports can restore
trust after a gap. A targeted report updates a known trusted baseline and
cannot stand in for a missing complete manifest. Unsolicited, stale, expired,
or identity-mismatched responses are ignored or rejected without mutation.
There is no production probe loop.

## Qualification

Pure qualification evaluates one P1-06 job against one immutable worker
snapshot. All required capabilities must exist on the same worker. The
effective set is:

```text
implemented report
  intersection credential authorization
  intersection registration policy
  intersection job and schema compatibility
  intersection valid healthy/degraded admission
  intersection effective capacity
  intersection capability and parameter requirements
```

Bounded reasons include `qualified`, `not_implemented`, `unauthorized`,
`incompatible`, `unhealthy`, `degraded_not_accepting`, `unavailable`,
`draining`, `disabled`, `unknown_or_stale`, `parameter_mismatch`,
`schema_mismatch`, `no_capacity`, `session_unavailable`, and
`probe_required`. Results include an epoch/digest race token but do not acquire
a lease.

## Capacity, reservations, and scheduling

Effective available capacity is conservative:

```text
min(reported available,
    max(0, total - controller active use - pending reservations))
```

A worker report cannot erase controller-observed use. A reservation is
ephemeral and scoped to worker instance, job, unique required capability set,
and qualification snapshot. It expires within a bounded interval, creates no
job, and is not a second queue.

The serialized controller flow qualifies, reserves, asks P1-07 for the
specific qualified job, binds the reservation to its lease, creates the SWWP
assignment, and converts the reservation to active use only after durable
acknowledgment. Failed acquisition, rejection, cancellation, terminal result,
reconciliation, timeout, disconnect, and close release the applicable
accounting. P1-07 remains durable queue and lease authority.

## Last-race rejection

A bounded capability-related `job_rejected` must match the current
session/lease/attempt. The controller immediately releases pre-acceptance
capacity and invokes P1-07's unacknowledged-lease reconciliation. It does not
record a handler failure outcome. The affected capability becomes unknown,
the stale qualification token cannot be reused, and a targeted or full probe
is required before rescheduling.

Deadline, cancellation, retry-attempt, and replay policies remain
repository-owned. Worker claims cannot rewrite authorization or schema policy.

## Health and lifecycle

The optional worker health component reads immutable registry snapshots. It
reports aggregate connected, qualified, unknown, stale, probe, capacity,
reservation, and active-assignment counts. Public health never exposes worker
or session IDs, epochs, digests, parameter values, voices, paths, payloads, or
dependency details.

`/healthz` remains minimal and probe-free. `/readyz` gates on worker
capabilities only when the composition explicitly declares them required. The
composition accepts at most 64 distinct required capability names and rejects a
larger set instead of silently truncating readiness evaluation. No simulated
workers is not labeled a production outage when none are required. Health
collection does not reserve, expire, or probe.

Drain immediately makes a worker unschedulable and clears pending
reservations. Existing durable leases remain under P1-07 reconciliation
authority. No unmanaged lifecycle task was added.

## Phase boundary

Phase 1 testing uses the real registry, qualifier, reservation service,
P1-07 scheduler/repository, SWWP session adapter, and deterministic in-memory
peers. Simulations cover proactive changes, capacity, duplicate/dropped/
reordered updates, gaps, digest corruption, probes, staleness, degradation,
recovery, reconnect, and rejection races.

Deferred work includes the controller-side live WSS endpoint, production worker
health servers, capability persistence, and container operation. P2-03 adds
bounded worker dependency probes and handler dispatch but does not move
artifact staging, media validation,
hash/identity/generation fences, promotion, result acceptance, Liquidsoap, and
publication use the P1-10 controller artifact boundary when integrated.
Capability qualification and capacity accounting do not make a result
authoritative: the artifact coordinator must validate, promote, and durably
commit through P1-07 first. P1-09 then releases active capacity exactly once.
Production worker handlers and broader broadcast-path migration remain later
work.
