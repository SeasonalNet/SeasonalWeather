# Artifact staging and result fencing

P1-10 provides the simulated-only controller boundary for artifact-producing
job results. It does not introduce a worker process, live SWWP transport,
shared container volume, TTS handler, or Liquidsoap integration.

## Authority and reference contract

A worker may write one completed file beneath its assigned staging namespace,
sync and close it, and return an `ArtifactResult`. The strict result contains
the durable job, lease, and attempt identities; independent result-schema
version; admitted configuration generation; applicable source, event/product,
and content identities; completion time; and a bounded `ArtifactReference`.
The reference contains an artifact class, relative staging token, claimed
SHA-256 and size, media type, and bounded media claims. Unknown fields,
arbitrary metadata, bytes, absolute paths, traversal, alternate separators,
control characters, URIs, and worker-selected active targets are rejected.

The controller injects the active-target policy and current generation,
source, event, and content authority. Worker claims never decide compatibility
or currency.

## Claim and immutable storage

The claim boundary rejects symlink path components, hard links, FIFOs,
devices, directories, sparse surprises, oversized files, and non-regular
input. It opens the leaf with no-follow and nonblocking descriptor semantics,
atomically renames it on the same filesystem into mode-0700
`.controller-pending`, verifies descriptor and renamed-inode identity, and
copies through the stable descriptor into a separately created
controller-owned inode. Hashing is streaming and every write is checked. File
identity is compared before and after the copy, so replacement, ordinary
growth, truncation, or concurrent writes fail closed. Workers must close their
file before reporting it; NFS/SMB exclusivity is not claimed.

The copied bytes are hashed again before content-addressed publication. Blobs
use:

```text
blobs/sha256/ab/cd/<64-lowercase-hex-digest>
```

All path components come from the controller-computed digest. A
destination-local exclusive temporary file is flushed, verified byte-for-byte
by digest and size, chmodded `0440`, and linked atomically. The containing
directory is fsynced. Existing identical content is reused; a link, non-file,
or different content at the digest path is corruption. Only bounded,
service-owned pending files are eligible for cleanup; general retention and
garbage collection are deferred.

## Media and fencing

WAV jobs are parsed only from the claimed controller copy. Validation checks
RIFF/WAVE readability, uncompressed PCM policy, signed PCM encoding, sample
width, channel count, sample rate, nonempty frame count, maximum duration,
the exact decoded byte count, and every supplied media claim. Truncated data
is rejected even when its header claims complete frames. Generic blobs still
receive type, size, and hash validation and never invoke a subprocess.

The immutable expected fence comes from the current durable assignment and
injected controller authority. Evaluation covers job type and ID, lease,
attempt, schema, deadline using controller time, artifact class, admitted and
current configuration generation, explicit compatibility, required
source/event/content identities, supersession, and replay policy. Missing
authoritative identity or unknown required generation requires revalidation.
Older generations are stale by default; only an injected compatibility
decision can accept them. `completed_at` cannot extend the deadline.

## Durable ordering and promotion

The state vocabulary is `prepared`, `promoted`, `committed`,
`reconciliation_required`, `superseded`, and `rejected`, with monotonic legal
transitions. The SQLite P1-07 repository stores only bounded metadata and
content references. Its `(job_id, attempt_id)` key is the idempotency identity,
and every digest, size, class, target-policy identity, prior-active digest,
result hash, timestamp, fence field, media claim, and provenance field is
conflict checked. SQLite transactions and WAL durability provide the journal;
there is no second JSON receipt authority.

The controller:

1. checks assignment/session authority and the cheap expected fence;
2. claims, hashes, and validates the staged bytes;
3. rechecks lifecycle admission and volatile fencing;
4. records a durable prepared intent, including prior-active identity;
5. inserts/verifies the immutable blob;
6. rechecks admission and fencing immediately before publication;
7. serializes by controller-selected target, creates and flushes a
   destination-local temporary, atomically replaces the target, fsyncs its
   directory, and verifies the resulting bytes;
8. records promotion;
9. invokes the P1-07 durable result commit;
10. records artifact commitment using that exact P1-07 result hash.

P1-08 emits `result_committed` only after step 10 returns. P1-09 releases the
active capacity only after that durable result path returns, and its release
operation is idempotent. An identical lost-ack resend returns the same durable
hash and commit time. A changed digest, size, target, identity, media claim,
completion time, or provenance is a conflict even after commitment.

## Crash reconciliation and lifecycle

Failure points are injectable after claim, prepared intent, blob insertion,
active replacement, and P1-07 result commit. `reconcile()` examines the durable
journal, recomputed blob identity, active-target identity, prior-active
identity, durable job status, and P1-07 result receipt. It can recover a
bounded matching pending controller copy, finish a prepared promotion for a
still-current leased/running attempt, or mark commitment only when the durable
job is succeeded with the matching P1-07 result hash. Contradictory evidence
does not replace the current active artifact and remains
`reconciliation_required`. Reconciliation is explicit and idempotent; no
background scanner or business-logic replay is created.

Drain admission is injected from the lifecycle owner and is checked before
claim and immediately before promotion. Once past the atomic-publication
boundary, completion either finishes or leaves durable reconciliation
evidence. `close()` is idempotent and blocks new work.

Artifact health reports only bounded aggregate storage availability, pending
reconciliation count, service-owned temporary backlog, required-active missing
count, service state, and a bounded reason identifier. It does not hash media,
scan unbounded directories, reconcile, mutate files, or disclose paths,
digests, job/worker/source identities, voices, or filenames. The subsystem is
not required in the default simulated-only runtime; deployments may explicitly
make it readiness-required.

## Existing paths deliberately not migrated

P1-10 establishes reusable authority and proves it through deterministic
integration tests. Existing direct writers remain unchanged:

- `broadcast/segment_store.py` and conductor WAV/index publication are deferred
  to P1-19/P1-20;
- TTS and SAME WAV helpers and alert origination are deferred to P1-16 and
  P1-19/P1-20;
- RWT/RMT, CAP ledger, station-feed, SAME targeting, injection-tool, and other
  operational publication paths retain their existing owners until their
  explicitly assigned later packets.

P1-11 configuration compilation, P1-15 reload/generation commits, P1-16 TTS
redesign, P1-19/P1-20 segment/alert migration, production workers/transports,
Liquidsoap mutation, object storage, NFS/SMB support, and Phase 2/3
infrastructure are not implemented here.
