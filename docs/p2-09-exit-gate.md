# P2-09 Phase 2 image and exit gate

P2-09 is the validation boundary for the completed Phase 2 image and worker
process work. It does not introduce Compose topology or deploy a service. The
gate combines the repository quality/test interface, the declared Buildx
image matrix, and inspection of each built image.

## Gate command

Run the gate with the repository virtual environment:

```bash
make PYTHON=./.venv/bin/python phase2-gate
```

This runs `make check`, builds the controller plus routine-worker, Piper,
legacy-TTS, maintenance, and development profiles through `make images`, and
then runs `tools.quality.phase2_exit_gate --images`.

Forgejo splits that combined interface across two ordered jobs for the same
commit. The ordinary `victus-fast` runner executes `make check`. After it
passes, the dedicated `victus-builder` runner executes `make phase2-images`,
which builds and inspects the matrix without repeating quality and tests.
Only the builder job runs `tools/ci/bootstrap_docker.sh`; it installs the
Docker CLI/Buildx when needed and requires the runner-provided `dind-builder`
endpoint. See [Forgejo Runner Docker access](forgejo-runner-docker.md). GitHub
keeps its native hosted Docker path and runs the combined `phase2-gate` target.

The source-only inspection is also available without Docker:

```bash
./.venv/bin/python -m tools.quality.phase2_exit_gate
```

## Built-image assertions

The gate inspects each image's effective user, entrypoint, healthcheck,
exposed ports, OCI labels, and embedded `/usr/share/seasonalweather/build-info.json`.
It compares immutable build identity, schema, protocol, and catalog fields
across profiles while allowing the image profile and profile-derived build ID
to differ.

Each image is executed with its entrypoint replaced by Python for bounded
read-only probes. The probes verify that the build-info CLI agrees with the
embedded record, every image can explain a catalog code without mutable state,
the complete catalog is identical across profiles, and package/dependency
boundaries hold: the controller has no worker package, and workers have no
controller API, database, broadcast, NWWS, FastAPI, SQLAlchemy, slixmpp, or
Uvicorn content.

The source portion rejects reintroduced `jobs.execution_mode` configuration,
controller construction of the retired `EmbeddedExecutionPort`, and routine
or maintenance job policies that bypass their dedicated worker executor.

## Exit-gate evidence

The gate is supplemental to the existing quality and test authorities. A
Phase 2 acceptance record must separately identify the requester-authoritative
ordinary-shell quality actuals and complete suite, plus the named Forgejo CI
result. Local Docker inspection and agent-run tests are supporting evidence;
they do not become requester acceptance by implication.

Compose startup, persistent-volume topology, deployment, restart, and
production worker operation remain Phase 3 concerns.
