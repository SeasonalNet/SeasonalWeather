# SeasonalWeather documentation

This index is organized by the question an operator or contributor is trying
to answer. The root [README](../README.md) is the short entry point; this page
points to the detailed contracts.

## Operators

- [Operator guide](operator-guide.md) — installation inputs, configuration,
  Compose commands, workers, TTS, logs, diagnostics, and recovery.
- [Build and provenance](build-and-provenance.md) — image targets, build
  records, compatibility, and native SAME tool compilation.
- [Production migration and Phase 3 gate](p3-08-production-migration.md) —
  reversible cutover, persistence, rollback, and acceptance evidence.
- [Staging operation](p3-07-staging.md) — isolated staging project and
  failure/soak boundaries.
- [Health and readiness](health-readiness.md)
- [Optional PostgreSQL preflight](p4-01-postgresql-preflight.md)
- [Lifecycle and shutdown](lifecycle-shutdown.md)
- [Runtime diagnostics](runtime-diagnostics.md)
- [Runtime wrappers](runtime-wrappers.md)

## Configuration and API

- [Configuration compiler](configuration-compiler.md)
- [Configuration validation](configuration-validation.md)
- [Configuration reload](configuration-reload.md)
- [Command and job contracts](command-job-contracts.md)
- [Durable job repository](durable-job-repository.md)
- [Quality and CI](quality-and-ci.md)

## Deployment architecture

- [Compose topology](p3-01-compose-topology.md)
- [Authority-separated volumes](p3-02-authority-separated-volumes.md)
- [Local-only TTS](p3-03-local-only-tts.md)
- [seasonal-ttsd integration](p3-04-seasonal-ttsd.md)
- [OpenAI-compatible TTS](p3-05-openai-compatible.md)
- [Filesystem and network boundaries](p2-04-filesystem-network.md)
- [Container security](p2-05-container-security.md)
- [Health/lifecycle image contract](p2-06-health-lifecycle.md)
- [Observability](p2-07-observability.md)
- [Phase 2 exit gate](p2-09-exit-gate.md)

## Runtime and protocol reference

- [Worker runtime](worker-runtime.md)
- [SWWP](swwp.md)
- [Worker capabilities](worker-capabilities.md)
- [Artifact staging](artifact-staging.md)
- [Segment registry](segment-registry.md)
- [Segment behavior](p1-20-segments.md)
- [Formatter subsystem](formatter-subsystem.md)
- [NWWS source adapter](nwws-source-adapter.md)
- [OFFNT2](offnt2.md)
- [Diagnostic catalog](diagnostic-catalog.md)
- [Diagnostic namespaces](diagnostic-namespaces.md)

## Historical and contributor material

- [Installer reference](INSTALLER.md) — legacy systemd/bare-metal deployment.
- [State machine](STATE_MACHINE.md)
- [JSON persistence migration](json-persistence-migration.md)
- [Quality baseline](quality-baseline-v0.17.0.md)
- [Release notes](RELEASES.md)
- [Project notes](NOTES.md)
- [Contributing](../CONTRIBUTING.md)
