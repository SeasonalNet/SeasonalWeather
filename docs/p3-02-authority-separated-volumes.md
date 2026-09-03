# P3-02 authority-separated volumes and artifact flow

P3-02 applies the existing controller-owned artifact service to the Phase 3
Compose topology. The controller remains the only service that owns durable
job state, validates worker results, and promotes artifacts into active
broadcast targets.

## Volume authority

| Volume | Controller | Routine/maintenance worker | Liquidsoap |
| --- | --- | --- | --- |
| `seasonalweather-state` | read/write | absent | absent |
| `seasonalweather-jobs` | read/write | absent | absent |
| `seasonalweather-artifacts` | read/write | read-only | read-only |
| `seasonalweather-artifact-staging` | read/write at staging path | read/write at staging path | absent |
| `seasonalweather-logs` | read/write | absent | absent |

The artifact root is mounted read-only in workers, while the separate staging
volume is mounted read/write only at:

```text
/var/lib/seasonalweather/artifacts/worker-artifacts/staging
```

This nested mount lets the controller see worker output on the same shared
staging volume without granting workers write access to controller-owned
blobs or active broadcast files. Liquidsoap receives only a read-only view of
the artifact root and never receives the staging volume as a writable mount.

The controller's artifact layout is:

```text
/var/lib/seasonalweather/artifacts/worker-artifacts/
  staging/  # shared worker result input
  blobs/    # controller-owned immutable content-addressed copies
  active/   # controller-owned promoted output
```

No worker receives operational state, the job database, or controller logs.
The service-level read-only image contract remains in force; the explicit
staging mount is the only worker write exception.

Both application images pre-create the staging mountpoint as UID/GID 10001.
The staging volume uses normal Docker copy-up so a new empty volume inherits
that ownership; `nocopy` would leave a fresh volume root-owned and unwritable
by the non-root worker. Existing volumes retain their existing ownership and
are not recursively changed during startup.

## Artifact flow

The initial transport is `shared-volume`. A worker writes its completed file
under its assigned staging namespace and returns only the bounded relative
artifact reference and result metadata through SWWP. Large artifact bytes do
not travel over SWWP.

The controller then uses the existing P1-10 path:

1. Claim the staged file safely and remove it from the worker staging
   namespace.
2. Recompute and verify its digest, size, and media claims.
3. Apply the durable job, lease, attempt, deadline, configuration, source,
   event, and content fences.
4. Store an immutable controller-owned blob.
5. Promote to the selected active target with same-directory atomic replace
   and directory durability.
6. Commit the durable result and retain reconciliation evidence on failure.

Stale, malformed, substituted, symlinked, non-regular, oversized, and
otherwise unauthorized artifacts remain rejected by the existing controller
artifact service. No worker-selected path can become an active target.

`SharedVolumeArtifactTransport` isolates this same-host layout from the
controller composition. A future short-lived authenticated transfer or
object-storage transport can provide the same storage-path contract without
changing SWWP job semantics or moving large bytes onto the control WebSocket.

## Recreation and recovery

The state, jobs, artifact, staging, and log volumes are named Compose volumes.
Container recreation therefore does not replace controller databases or
accepted artifact bytes. Artifact recovery continues to use the existing
durable publication journal and bounded reconciliation behavior; Compose does
not add a second receipt or artifact authority.
