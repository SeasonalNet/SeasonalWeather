from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import inspect
import logging
import signal
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn

from seasonalweather import __version__

from ..artifacts.composition import build_controller_artifact_composition
from ..auth import AuthenticationRepository, AuthenticationService
from ..broadcast.segment_service import SegmentApplicationService
from ..broadcast.station_feed_runtime import set_diagnostic_sink as set_station_feed_diagnostic_sink
from ..build_metadata import BuildInfo, current_build_info
from ..build_metadata.compatibility import BuildCompatibilityError, ensure_runtime_compatibility
from ..capabilities.service import CapabilitySchedulerService, declared_capability_names
from ..commands import CommandStore
from ..config import AuthMode, load_config
from ..configuration_reload.candidate_store import CandidateStore
from ..configuration_reload.resources import OrchestratorResourcePreparer
from ..configuration_reload.safe_point import SafePointCoordinator, orchestrator_blockers
from ..configuration_reload.service import ConfigurationReloadService
from ..configuration_reload.validation_job import ValidationJobRunner
from ..control import OrchestratorControl
from ..database.bootstrap import bootstrap_database_from_config
from ..database.configuration_reload import ReloadRepository
from ..diagnostics.bindings import FOUNDATION_CODES, OBS_CODES, RELOAD_CODES, RUNTIME_CODES, SEGMENT_CODES
from ..health_service import build_runtime_health_service
from ..job_store import (
    CommandJobCoordinator,
    DurableJobService,
    JobDatabase,
    JobRepository,
    JobScheduler,
)
from ..jobs.policies import JobType, QueueClass
from ..jobs.worker_client import WorkerSynthesisClient
from ..lifecycle import Lifecycle, LifecycleState, TaskSupervisor
from ..lifecycle_records import LifecycleRecordWriter, LifecycleStage
from ..logging_config import set_runtime_diagnostic_sink
from ..main import Orchestrator, _setup_logging
from ..nwws.diagnostics import NwwsRuntimeDiagnosticSink
from ..observability import WorkerTelemetryMetricsPort, create_default_metrics, set_correlation
from ..runtime_diagnostics.fatal import FatalBoundary, SecondaryFailureLedger, enable_faulthandler
from ..runtime_diagnostics.marker import ProcessMarkerStore, controller_marker
from ..runtime_diagnostics.models import CorrelationContext, DiagnosticRole, PromotionReason
from ..runtime_diagnostics.repository import OccurrenceRepository
from ..runtime_diagnostics.service import RuntimeDiagnosticService
from ..runtime_diagnostics.sink import RuntimeDiagnosticSink
from ..runtime_diagnostics.worker import WorkerDiagnosticTranslator
from ..swwp.adapter import JobStoreSwwpAdapter
from ..swwp.auth import BearerTokenRegistrationPolicy
from ..swwp.constants import DEFAULT_LIMITS, ProtocolLimits
from .api import create_app
from .worker_sessions import LiveWorkerSession, LiveWorkerSessionManager, WorkerSocket

log = logging.getLogger("seasonalweather.api")

__all__ = ["__version__"]


def _operational_state_root(cfg: Any) -> Path:
    configured = str(getattr(cfg.paths, "operational_state_dir", "") or "").strip()
    if configured:
        return Path(configured)
    database_path = str(getattr(cfg.database, "path", "") or "").strip()
    if database_path:
        return Path(database_path).parent
    return Path(cfg.paths.work_dir)


def _artifact_root(cfg: Any) -> Path:
    configured = str(getattr(cfg.paths, "artifact_dir", "") or "").strip()
    return Path(configured or cfg.paths.work_dir)


class _ControllerOwnedUvicornServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:
        """The SeasonalWeather controller is the sole signal owner."""


@dataclass
class _MarkerLifecycleIntegration:
    store: ProcessMarkerStore
    secondary_failures: SecondaryFailureLedger
    failure: BaseException | None = None
    _failure_event: asyncio.Event = field(default_factory=asyncio.Event)

    def transition(self, state: LifecycleState) -> None:
        if state is LifecycleState.STOPPED or self.failure is not None:
            return
        try:
            self.store.update_stage(state.value)
        except BaseException as exc:
            self._retain("process_marker_stage_update_failed", exc)

    def terminal_failure(self) -> RuntimeError | None:
        if self.failure is None:
            return None
        return RuntimeError("controller process marker integration failed")

    async def wait_for_failure(self) -> None:
        await self._failure_event.wait()

    def finalize_clean(self) -> None:
        if self.failure is not None:
            raise RuntimeError("controller process marker integration failed")
        try:
            self.store.update_stage(LifecycleState.STOPPED.value)
        except BaseException as exc:
            self._retain("process_marker_stopped_update_failed", exc)
            raise RuntimeError("controller process marker finalization failed") from None
        try:
            self.store.mark_clean()
        except BaseException as exc:
            self._retain("process_marker_clean_failed", exc)
            raise RuntimeError("controller process marker finalization failed") from None

    def _retain(self, event: str, error: BaseException) -> None:
        if self.failure is None:
            self.failure = error
            self._failure_event.set()
        self.secondary_failures.retain(event, error)


def _build_job_service(
    cfg: Any,
    lifecycle: Lifecycle,
    *,
    diagnostic_sink: object | None = None,
) -> DurableJobService | None:
    if not cfg.jobs.enabled:
        return None
    job_database = JobDatabase(
        path=cfg.jobs.path,
        busy_timeout_ms=cfg.jobs.busy_timeout_ms,
    )
    job_repository = JobRepository(
        job_database,
        payload_max_bytes=cfg.jobs.payload_max_bytes,
        result_max_bytes=cfg.jobs.result_max_bytes,
        progress_retention=cfg.jobs.progress_retention,
        event_retention=cfg.jobs.event_retention,
    )
    return DurableJobService(
        job_repository,
        lifecycle,
        reconciliation_batch_size=cfg.jobs.reconciliation_batch_size,
        diagnostic_sink=diagnostic_sink,
    )


def _build_auth_service(cfg: Any, database: Any) -> AuthenticationService | None:
    exchange_enabled = cfg.api.auth.mode in {AuthMode.EXCHANGE, AuthMode.HYBRID}
    if exchange_enabled and database is None:
        raise RuntimeError("Exchange authentication requires the controller SQLite database.")
    if not exchange_enabled:
        return None
    return AuthenticationService(
        AuthenticationRepository(database),
        cfg.api.auth.exchange,
    )


async def _initialize_job_service(
    job_service: DurableJobService | None,
    command_store: CommandStore,
    *,
    database_available: bool,
    reconciliation_batch_size: int,
) -> None:
    if job_service is None:
        return
    await asyncio.to_thread(job_service.initialize)
    if database_available:
        await CommandJobCoordinator(
            job_service.repository,
            command_store,
        ).repair(limit=reconciliation_batch_size)


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    callback: Callable[[], None],
) -> Callable[[], None]:
    installed: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, callback)
        installed.append(sig)

    def remove() -> None:
        for sig in installed:
            loop.remove_signal_handler(sig)

    return remove


def _install_loop_exception_handler(
    loop: asyncio.AbstractEventLoop,
    supervisor: TaskSupervisor,
) -> Callable[[], None]:
    prior = loop.get_exception_handler()

    def handle(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exception = context.get("exception")
        if not isinstance(exception, BaseException):
            exception = RuntimeError(str(context.get("message") or "unhandled event-loop failure")[:256])
        supervisor.report_background_failure(exception)

    loop.set_exception_handler(handle)

    def remove() -> None:
        loop.set_exception_handler(prior)

    return remove


def _prepare_runtime_diagnostics(
    *,
    database: Any,
    marker_store: ProcessMarkerStore,
    supervisor: TaskSupervisor,
    instance_id: str,
    build_info: BuildInfo | None = None,
) -> tuple[RuntimeDiagnosticService | None, CorrelationContext]:
    identity = (build_info or current_build_info()).build_identity
    service = RuntimeDiagnosticService(OccurrenceRepository(database)) if database is not None else None
    context = CorrelationContext(
        role=DiagnosticRole.CONTROLLER,
        instance_id=instance_id,
        component="controller",
        build_identity=identity,
    )
    if service is None:
        return None, context
    service.initialize()
    marker_store.reconcile_pending(service, current_context=context)
    try:
        service.prune_resolved(retention_days=90, retain_resolved=1_000)
    except Exception:
        log.warning("runtime_diagnostic_pruning_failed")
    supervisor.optional_failure = lambda name, exc: _promote_optional_failure(
        service,
        context,
        name,
        exc,
    )
    return service, context


def _promote_optional_failure(
    service: RuntimeDiagnosticService,
    controller_context: CorrelationContext,
    name: str,
    exception: BaseException,
) -> None:
    instance = service.build(
        code=RUNTIME_CODES["optional_task_degraded"],
        context=CorrelationContext(
            role=DiagnosticRole.CONTROLLER,
            instance_id=controller_context.instance_id,
            component=name[:64],
            build_identity=controller_context.build_identity,
            reason_code="optional_task_failed",
        ),
        message=f"Optional supervised task {name[:64]} failed.",
        operational_effect="The optional component is degraded for this controller process.",
        recovery_action="Inspect the bounded exception evidence and recover the component.",
        promotion_reason=PromotionReason.DEGRADATION,
        exception=exception,
    )
    service.promote(instance)


def _reload_diagnostic_promoter(
    service: RuntimeDiagnosticService | None,
    controller_context: CorrelationContext,
) -> Callable[[str, str, BaseException], None]:
    def promote(code: str, component: str, exception: BaseException) -> None:
        if service is None:
            return
        if code == RELOAD_CODES["retirement_pending"]:
            reason = PromotionReason.DEGRADATION
        elif code == RELOAD_CODES["safe_point_timeout"]:
            reason = PromotionReason.OPERATOR_ATTENTION
        else:
            reason = PromotionReason.RECONCILIATION
        try:
            service.promote(
                service.build(
                    code=code,
                    context=CorrelationContext(
                        role=DiagnosticRole.CONTROLLER,
                        instance_id=controller_context.instance_id,
                        component=component,
                        build_identity=controller_context.build_identity,
                        reason_code="configuration_reload_failure",
                    ),
                    message="Transactional configuration reload requires operational attention.",
                    operational_effect="The reload audit records whether the old or new generation remains active.",
                    recovery_action="Inspect the bounded reload audit and follow the catalog recovery guidance.",
                    promotion_reason=reason,
                    exception=exception,
                )
            )
        except BaseException:
            log.exception("configuration_reload_diagnostic_promotion_failed")

    return promote


async def run_api_server(*, config_path: str, host: str | None = None, port: int | None = None) -> None:
    instance_id = f"controller_{uuid.uuid4().hex}"
    set_runtime_diagnostic_sink(None)
    try:
        build_info = current_build_info()
        ensure_runtime_compatibility(build_info, role="controller")
    except BuildCompatibilityError as exc:
        logging.getLogger("seasonalweather.build").critical(
            "Build identity is incompatible with the controller runtime.",
            extra={
                "event": "build_compatibility_rejected",
                "code": FOUNDATION_CODES["build.compatibility_rejected"],
                "reason": str(exc),
            },
        )
        raise
    except Exception:
        logging.getLogger("seasonalweather.build").critical(
            "Build identity metadata could not be loaded.",
            extra={"event": "build_identity_invalid", "code": FOUNDATION_CODES["build.identity_invalid"]},
            exc_info=True,
        )
        raise
    context = CorrelationContext(
        role=DiagnosticRole.CONTROLLER,
        instance_id=instance_id,
        component="controller",
        build_identity=build_info.build_identity,
    )
    secondary_failures = SecondaryFailureLedger()
    fatal = [FatalBoundary(None, context, secondary_failures)]
    enable_faulthandler()
    try:
        await _run_api_server_impl(
            config_path=config_path,
            host=host,
            port=port,
            instance_id=instance_id,
            fatal=fatal,
            build_info=build_info,
        )
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except BaseException as exc:
        fatal[0].report(exc)
        raise
    finally:
        set_runtime_diagnostic_sink(None)


async def _run_api_server_impl(
    *,
    config_path: str,
    host: str | None,
    port: int | None,
    instance_id: str,
    fatal: list[FatalBoundary],
    build_info: BuildInfo | None = None,
) -> None:
    build_info = build_info or current_build_info()
    lifecycle_records = LifecycleRecordWriter(
        role="controller",
        instance_id=instance_id,
        build_info=build_info,
    )
    lifecycle_records.startup_identity()
    lifecycle_records.stage(LifecycleStage.SERVICE_STARTING, ready=False)
    cfg = load_config(config_path)
    lifecycle_records.stage(LifecycleStage.CONFIGURATION_VALIDATED, ready=False)
    api_network = getattr(getattr(cfg, "network", None), "api", None)
    effective_host = str(host or getattr(api_network, "bind_host", "127.0.0.1"))
    effective_port = int(port if port is not None else getattr(api_network, "port", 9080))
    metrics_registry = create_default_metrics()
    _setup_logging(
        cfg,
        role="controller",
        instance_id=instance_id,
        build_info=build_info,
        metrics=metrics_registry,
    )
    set_correlation(
        role="controller",
        instance_id=instance_id,
        build_id=build_info.build_id,
        build_identity=build_info.build_identity,
    )
    log.info(
        "lifecycle_event=service_starting role=controller build=%s software=%s profile=%s target_platform=%s dirty_tree=%s",
        build_info.build_identity,
        build_info.software_version,
        build_info.image_profile,
        build_info.target_platform,
        str(build_info.dirty_tree).lower(),
    )
    state_root = _operational_state_root(cfg)
    db = bootstrap_database_from_config(cfg) if getattr(cfg.database, "enabled", True) else None
    try:
        marker_store = ProcessMarkerStore(state_root, database=db)
    except TypeError as exc:
        # Keep narrow compatibility with injected lifecycle test doubles from
        # before the SQLite marker repository was introduced.
        if "database" not in str(exc):
            raise
        marker_store = ProcessMarkerStore(state_root)
    marker_store.start(controller_marker(instance_id=instance_id))
    marker_integration = _MarkerLifecycleIntegration(
        marker_store,
        fatal[0].secondary_failures,
    )
    lifecycle = Lifecycle(
        cfg.lifecycle,
        transition_callback=marker_integration.transition,
    )
    supervisor = TaskSupervisor(lifecycle)
    orch = Orchestrator(
        cfg,
        lifecycle=lifecycle,
        supervisor=supervisor,
        lifecycle_records=lifecycle_records,
    )
    segment_service = None
    if all(hasattr(orch, name) for name in ("segment_registry", "_seg_store", "refresher", "conductor")):
        segment_service = SegmentApplicationService(
            registry=lambda: orch.segment_registry,
            store=orch._seg_store,
            refresher=orch.refresher,
            mode=lambda: orch.mode,
            supervisor=supervisor,
            runtime_snapshot=orch.conductor.inspection_snapshot,
        )
    control = OrchestratorControl(orch, config_path=config_path, segment_service=segment_service)
    diagnostic_service, context = _prepare_runtime_diagnostics(
        database=db,
        marker_store=marker_store,
        supervisor=supervisor,
        instance_id=instance_id,
        build_info=build_info,
    )
    foundation_sink: Callable[[str], RuntimeDiagnosticSink] | None = None
    if diagnostic_service is not None:
        orch.nwws_diagnostic_sink = NwwsRuntimeDiagnosticSink(
            diagnostic_service,
            context,
            generation_provider=lambda: orch.configuration_generation,
        )

        def build_foundation_sink(prefix: str) -> RuntimeDiagnosticSink:
            return RuntimeDiagnosticSink(
                diagnostic_service,
                context,
                codes={key: code for key, code in FOUNDATION_CODES.items() if key.startswith(prefix)},
                generation_provider=lambda: orch.configuration_generation,
            )

        foundation_sink = build_foundation_sink
        orch.cap_diagnostic_sink = build_foundation_sink("cap.")
        orch.ern_diagnostic_sink = build_foundation_sink("ern.")
        orch.database_diagnostic_sink = build_foundation_sink("database.")
        if hasattr(orch, "postgresql_preflight"):
            orch.postgresql_preflight.set_diagnostic_sink(orch.database_diagnostic_sink)
        set_station_feed_diagnostic_sink(orch.database_diagnostic_sink)
        if hasattr(orch, "alert_tracker"):
            orch.alert_tracker._diagnostic_sink = orch.database_diagnostic_sink
        orch.liquidsoap_diagnostic_sink = build_foundation_sink("liquidsoap.")
        if hasattr(orch, "conductor"):
            orch.conductor._diagnostic_sink = orch.liquidsoap_diagnostic_sink
        if hasattr(orch, "refresher"):
            orch.refresher._diagnostic_sink = RuntimeDiagnosticSink(
                diagnostic_service,
                context,
                codes=SEGMENT_CODES,
                generation_provider=lambda: orch.configuration_generation,
            )
        set_runtime_diagnostic_sink(
            RuntimeDiagnosticSink(
                diagnostic_service,
                context,
                codes=OBS_CODES,
                generation_provider=lambda: orch.configuration_generation,
            )
        )
        if hasattr(orch, "db_housekeeper"):
            housekeeper = orch.db_housekeeper
            if housekeeper is not None:
                housekeeper._diagnostic_sink = orch.database_diagnostic_sink
    fatal[0] = FatalBoundary(
        diagnostic_service,
        context,
        fatal[0].secondary_failures,
    )
    job_service = _build_job_service(
        cfg,
        lifecycle,
        diagnostic_sink=(foundation_sink("job.") if foundation_sink is not None else None),
    )
    auth_service = _build_auth_service(cfg, db)
    command_store = CommandStore(
        database=db,
        lifecycle=lifecycle,
    )
    segment_store = getattr(orch, "_seg_store", None)
    if segment_store is not None:
        await segment_store.reconcile_committed_refresh_commands(command_store)
    if segment_service is not None:
        await segment_service.reconcile_orphaned_refreshes(command_store)
    await _initialize_job_service(
        job_service,
        command_store,
        database_available=db is not None,
        reconciliation_batch_size=cfg.jobs.reconciliation_batch_size,
    )
    artifact_composition = None
    swwp_manager = LiveWorkerSessionManager()
    swwp_session_factory = None
    if job_service is not None:
        artifact_composition = build_controller_artifact_composition(
            orch,
            job_service.repository,
            work_root=_artifact_root(cfg),
            maximum_bytes=cfg.jobs.result_max_bytes,
        )
        orch.artifact_service = artifact_composition.service
        orch.artifact_results = artifact_composition.results
        orch.worker_job_service = job_service
        orch.worker_repository = job_service.repository
        orch.worker_active_root = artifact_composition.transport.paths.active
        if isinstance(orch.synthesizer, WorkerSynthesisClient):
            orch.synthesizer.bind(
                job_service=job_service,
                repository=job_service.repository,
                active_root=artifact_composition.transport.paths.active,
                configuration_generation=orch.configuration_generation,
            )
        scheduler = JobScheduler(
            job_service.repository,
            lifecycle,
            lease_seconds=cfg.jobs.lease_seconds,
            acknowledgment_seconds=cfg.jobs.assignment_ack_seconds,
        )
        durable_swwp = JobStoreSwwpAdapter(
            scheduler,
            job_service.repository,
            artifact_results=artifact_composition.results,
        )
        capability_swwp = CapabilitySchedulerService(
            orch.capability_registry,
            durable_swwp,
            clock=lambda: dt.datetime.now(dt.UTC),
            id_factory=lambda prefix: f"{prefix}-{uuid.uuid4().hex[:20]}",
        )
        diagnostic_swwp = (
            WorkerDiagnosticTranslator(diagnostic_service, instance_id) if diagnostic_service is not None else None
        )
        telemetry_swwp = WorkerTelemetryMetricsPort(metrics_registry)
        allowed_job_types = frozenset(job_type for job_type in JobType if not job_type.value.startswith("control."))
        allowed_queues = frozenset({QueueClass.ROUTINE, QueueClass.MAINTENANCE})
        allowed_capabilities = declared_capability_names()

        def make_swwp_session(websocket: WorkerSocket) -> LiveWorkerSession:
            authorization = str(websocket.headers.get("authorization", ""))
            presented = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
            policy = BearerTokenRegistrationPolicy(
                expected_token=cfg.secrets.worker_token,
                presented_token=presented,
                queues=allowed_queues,
                job_types=allowed_job_types,
                capabilities=allowed_capabilities,
            )
            swwp_cfg = cfg.network.swwp
            return LiveWorkerSession(
                websocket,
                durable=durable_swwp,
                policy=policy,
                capabilities=capability_swwp,
                diagnostics=diagnostic_swwp,
                telemetry=telemetry_swwp,
                heartbeat_interval_seconds=swwp_cfg.heartbeat_interval_seconds,
                heartbeat_timeout_seconds=swwp_cfg.heartbeat_timeout_seconds,
                lease_seconds=swwp_cfg.lease_seconds,
                assignment_ack_seconds=swwp_cfg.assignment_ack_seconds,
                controller_epoch=max(1, int(dt.datetime.now(dt.UTC).timestamp())),
                limits=ProtocolLimits(
                    max_message_bytes=swwp_cfg.max_message_bytes,
                    max_string_chars=DEFAULT_LIMITS.max_string_chars,
                    max_collection_items=DEFAULT_LIMITS.max_collection_items,
                    max_map_items=DEFAULT_LIMITS.max_map_items,
                    max_depth=DEFAULT_LIMITS.max_depth,
                    max_version_entries=DEFAULT_LIMITS.max_version_entries,
                    max_heartbeat_leases=DEFAULT_LIMITS.max_heartbeat_leases,
                    max_reconciliation_items=DEFAULT_LIMITS.max_reconciliation_items,
                    max_retained_errors=DEFAULT_LIMITS.max_retained_errors,
                    min_heartbeat_seconds=DEFAULT_LIMITS.min_heartbeat_seconds,
                    max_heartbeat_seconds=DEFAULT_LIMITS.max_heartbeat_seconds,
                ),
            )

        swwp_session_factory = make_swwp_session
    reload_service = None
    if db is not None and job_service is not None:
        candidate_store = CandidateStore(_operational_state_root(cfg) / "configuration-candidates", database=db)
        reload_service = ConfigurationReloadService(
            config_path=config_path,
            candidate_store=candidate_store,
            repository=ReloadRepository(db),
            command_store=command_store,
            validation_jobs=ValidationJobRunner(candidate_store, job_service),
            resource_preparer=OrchestratorResourcePreparer(orch, orch.reload_activities),
            safe_points=SafePointCoordinator(
                orch.reload_activities,
                external_blockers=lambda: orchestrator_blockers(orch),
            ),
            active_configuration=orch.cfg,
            supervisor=supervisor,
            diagnostic_promoter=_reload_diagnostic_promoter(diagnostic_service, context),
        )
        await reload_service.reconcile_startup()
    health_service = build_runtime_health_service(
        orch,
        command_store=command_store,
        auth_service=auth_service,
        job_service=job_service,
        capability_registry=getattr(orch, "capability_registry", None),
        swwp_manager=swwp_manager,
        required_capabilities=(
            ("tts.synthesis.v1", "audio.alert_artifact.v1")
            if str(getattr(getattr(cfg, "tts", None), "backend", "local")) == "local"
            else ()
        ),
    )
    app = create_app(
        control,
        store=command_store,
        auth_service=auth_service,
        health_service=health_service,
        lifecycle=lifecycle,
        reload_service=reload_service,
        diagnostic_service=diagnostic_service,
        build_info=build_info,
        metrics=metrics_registry,
        instance_id=instance_id,
        swwp_manager=swwp_manager,
        swwp_session_factory=swwp_session_factory,
        swwp_path=str(
            getattr(
                getattr(getattr(cfg, "network", None), "swwp", None),
                "controller_path",
                "/v1/workers/connect",
            )
        ),
    )
    lifecycle_records.stage(LifecycleStage.CONTROL_PLANE_READY, ready=False)

    server = _ControllerOwnedUvicornServer(
        uvicorn.Config(
            app,
            host=effective_host,
            port=effective_port,
            log_level="info",
            proxy_headers=False,
            forwarded_allow_ips="",
            timeout_graceful_shutdown=max(
                1,
                int(cfg.lifecycle.active_request_seconds),
            ),
        )
    )
    # The controller below is the sole signal owner. Uvicorn remains
    # responsible for its supported active-request drain contract.

    def request_shutdown() -> None:
        if lifecycle.request_shutdown():
            lifecycle_records.stage(LifecycleStage.SERVICE_DRAINING, ready=False, reason="signal")
        server.should_exit = True

    remove_signal_handlers = _install_signal_handlers(
        asyncio.get_running_loop(),
        request_shutdown,
    )
    remove_loop_exception_handler = _install_loop_exception_handler(
        asyncio.get_running_loop(),
        supervisor,
    )
    terminal_failure: BaseException | None = None
    try:
        terminal_failure = await _run_controller_session(
            server=server,
            supervisor=supervisor,
            lifecycle=lifecycle,
            orch=orch,
            database=db,
            job_service=job_service,
            cfg=cfg,
            swwp_manager=swwp_manager,
            marker_integration=marker_integration,
            secondary_failures=fatal[0].secondary_failures,
        )
    except BaseException as exc:
        terminal_failure = terminal_failure or exc

    handler_failures = _remove_controller_handlers(
        remove_loop_exception_handler,
        remove_signal_handlers,
        fatal[0].secondary_failures,
    )
    if terminal_failure is not None:
        if lifecycle_records.last_stage not in {
            LifecycleStage.SERVICE_READY,
            LifecycleStage.SERVICE_STARTED_DEGRADED,
            LifecycleStage.SERVICE_DRAINING,
        }:
            lifecycle_records.stage(
                LifecycleStage.SERVICE_STARTED_DEGRADED,
                ready=False,
                reason="controller_failed",
            )
        raise terminal_failure
    if handler_failures:
        raise RuntimeError("controller handler cleanup failed")
    marker_integration.finalize_clean()
    lifecycle.mark_stopped()
    lifecycle_records.stage(LifecycleStage.SERVICE_STOPPED, ready=False)


async def _run_controller_session(
    *,
    server: _ControllerOwnedUvicornServer,
    supervisor: TaskSupervisor,
    lifecycle: Lifecycle,
    orch: Orchestrator,
    database: Any,
    job_service: DurableJobService | None,
    cfg: Any,
    swwp_manager: LiveWorkerSessionManager,
    marker_integration: _MarkerLifecycleIntegration,
    secondary_failures: SecondaryFailureLedger,
) -> BaseException | None:
    api_task = supervisor.create_task(
        server.serve(),
        name="seasonalweather-api",
        required=True,
    )
    supervisor.create_task(
        orch.run(),
        name="seasonalweather-orchestrator",
        required=True,
    )
    await _wait_for_shutdown_or_marker_failure(lifecycle, marker_integration)
    server.should_exit = True
    primary_failure = await supervisor.wait_for_fatal() if lifecycle.state is LifecycleState.FAILED else None

    cleanup_failures: tuple[tuple[str, BaseException], ...] = ()
    shutdown_failure: BaseException | None = None
    try:
        cleanup_failures = await asyncio.wait_for(
            _shutdown_controller(
                lifecycle=lifecycle,
                supervisor=supervisor,
                api_task=api_task,
                orch=orch,
                database=database,
                job_service=job_service,
                cfg=cfg,
                swwp_manager=swwp_manager,
            ),
            timeout=cfg.lifecycle.total_seconds,
        )
    except TimeoutError as exc:
        log.error("controller_shutdown_deadline_exceeded")
        secondary_failures.retain("controller_shutdown_deadline_exceeded", exc)
        shutdown_failure = RuntimeError("controller shutdown deadline exceeded")
    except BaseException as exc:
        secondary_failures.retain("controller_shutdown_failed", exc)
        shutdown_failure = exc
    for event, failure in cleanup_failures:
        secondary_failures.retain(event, failure)
    return _terminal_controller_failure(
        primary_failure=primary_failure,
        shutdown_failure=shutdown_failure,
        marker_failure=marker_integration.terminal_failure(),
        cleanup_failed=bool(cleanup_failures),
    )


async def _wait_for_shutdown_or_marker_failure(
    lifecycle: Lifecycle,
    marker_integration: _MarkerLifecycleIntegration,
) -> None:
    shutdown_wait = asyncio.create_task(
        lifecycle.wait_for_shutdown(),
        name="controller-shutdown-wait",
    )
    marker_wait = asyncio.create_task(
        marker_integration.wait_for_failure(),
        name="process-marker-failure-wait",
    )
    try:
        done, _ = await asyncio.wait(
            {shutdown_wait, marker_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if marker_wait in done and not shutdown_wait.done():
            lifecycle.request_shutdown()
        await shutdown_wait
    finally:
        for task in (shutdown_wait, marker_wait):
            if not task.done():
                task.cancel()
        await asyncio.gather(shutdown_wait, marker_wait, return_exceptions=True)


def _terminal_controller_failure(
    *,
    primary_failure: BaseException | None,
    shutdown_failure: BaseException | None,
    marker_failure: BaseException | None,
    cleanup_failed: bool,
) -> BaseException | None:
    if primary_failure is not None:
        return primary_failure
    if shutdown_failure is not None:
        return shutdown_failure
    if marker_failure is not None:
        return marker_failure
    if cleanup_failed:
        return RuntimeError("controller shutdown cleanup failed")
    return None


async def _shutdown_controller(
    *,
    lifecycle: Lifecycle,
    supervisor: TaskSupervisor,
    api_task: asyncio.Task[object],
    orch: Orchestrator,
    database: Any,
    job_service: DurableJobService | None,
    cfg: Any,
    swwp_manager: LiveWorkerSessionManager,
) -> tuple[tuple[str, BaseException], ...]:
    if not lifecycle.force_requested and not api_task.done():
        await _wait_task_or_force(
            lifecycle,
            api_task,
            timeout=cfg.lifecycle.active_request_seconds,
        )
    if not lifecycle.force_requested:
        alert_idle = await _wait_alert_or_force(
            lifecycle,
            orch,
            timeout=cfg.lifecycle.tts_stop_seconds,
        )
        if alert_idle is False:
            log.warning("alert_audio_drain_timeout")
    if not lifecycle.force_requested:
        publication_idle = await _wait_publication_or_force(
            lifecycle,
            orch,
            timeout=cfg.lifecycle.publication_seconds,
        )
        if publication_idle is False:
            log.warning("publication_fence_timeout")
    if lifecycle.state is LifecycleState.DRAINING:
        await swwp_manager.drain(
            deadline_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=cfg.lifecycle.active_request_seconds),
            reason="controller_shutdown",
        )
        lifecycle.mark_stopping()
    await supervisor.stop()
    return await _close_resources(
        orch=orch,
        database=database,
        job_service=job_service,
        job_close_timeout_seconds=cfg.jobs.shutdown_reconciliation_seconds,
        timeout_seconds=cfg.lifecycle.resource_close_seconds,
        tts_timeout_seconds=cfg.lifecycle.tts_stop_seconds,
    )


async def _wait_task_or_force(
    lifecycle: Lifecycle,
    task: asyncio.Task[object],
    *,
    timeout: float,
) -> None:
    force_task = asyncio.create_task(
        lifecycle.wait_for_force(),
        name="lifecycle-force-wait",
    )
    try:
        await asyncio.wait(
            {task, force_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        force_task.cancel()
        await asyncio.gather(force_task, return_exceptions=True)


async def _wait_publication_or_force(
    lifecycle: Lifecycle,
    orch: Orchestrator,
    *,
    timeout: float,
) -> bool | None:
    publication_task = asyncio.create_task(
        orch.publication_fence.wait_idle(timeout),
        name="publication-fence-wait",
    )
    force_task = asyncio.create_task(
        lifecycle.wait_for_force(),
        name="lifecycle-force-wait",
    )
    try:
        done, _ = await asyncio.wait(
            {publication_task, force_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if publication_task in done:
            return publication_task.result()
        return None
    finally:
        for task in (publication_task, force_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            publication_task,
            force_task,
            return_exceptions=True,
        )


async def _wait_alert_or_force(
    lifecycle: Lifecycle,
    orch: Orchestrator,
    *,
    timeout: float,
) -> bool | None:
    alert_task = asyncio.create_task(
        orch.alert_audio.wait_idle(timeout),
        name="alert-audio-drain-wait",
    )
    force_task = asyncio.create_task(
        lifecycle.wait_for_force(),
        name="lifecycle-force-wait",
    )
    try:
        done, _ = await asyncio.wait(
            {alert_task, force_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if alert_task in done:
            return alert_task.result()
        return None
    finally:
        for task in (alert_task, force_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            alert_task,
            force_task,
            return_exceptions=True,
        )


def _remove_controller_handlers(
    remove_loop_exception_handler: Callable[[], None],
    remove_signal_handlers: Callable[[], None],
    secondary_failures: SecondaryFailureLedger,
) -> tuple[BaseException, ...]:
    failures: list[BaseException] = []
    for event, remove in (
        ("loop_exception_handler_removal_failed", remove_loop_exception_handler),
        ("signal_handler_removal_failed", remove_signal_handlers),
    ):
        try:
            remove()
        except BaseException as exc:
            secondary_failures.retain(event, exc)
            failures.append(exc)
    return tuple(failures)


async def _close_resources(
    *,
    orch: Orchestrator,
    database,
    job_service,
    job_close_timeout_seconds: float,
    timeout_seconds: float,
    tts_timeout_seconds: float,
) -> tuple[tuple[str, BaseException], ...]:
    failures: list[tuple[str, BaseException]] = []
    try:
        await asyncio.wait_for(
            orch.api.aclose(),
            timeout=timeout_seconds,
        )
    except Exception as exc:
        log.warning("controller_resource_close_failed resource=nws_api")
        failures.append(("nws_api_close_failed", exc))
    if job_service is not None:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(job_service.close),
                timeout=job_close_timeout_seconds,
            )
        except Exception as exc:
            log.warning("controller_resource_close_failed resource=job_repository")
            failures.append(("job_repository_close_failed", exc))
    if database is not None:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(database.checkpoint),
                timeout=timeout_seconds,
            )
        except Exception as exc:
            log.warning("controller_resource_close_failed resource=sqlite")
            failures.append(("sqlite_checkpoint_failed", exc))
    try:
        loop: Any = asyncio.get_running_loop()
        await _shutdown_default_executor(
            loop,
            timeout_seconds=tts_timeout_seconds,
        )
    except Exception as exc:
        log.warning("controller_resource_close_failed resource=executor")
        failures.append(("executor_shutdown_failed", exc))
    return tuple(failures)


async def _shutdown_default_executor(
    loop: Any,
    *,
    timeout_seconds: float,
) -> None:
    shutdown = loop.shutdown_default_executor
    try:
        supports_timeout = "timeout" in inspect.signature(shutdown).parameters
    except (TypeError, ValueError):
        supports_timeout = False
    if supports_timeout:
        await shutdown(timeout=timeout_seconds)
        return
    await asyncio.wait_for(shutdown(), timeout=timeout_seconds)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=("Run the SeasonalWeather controller and configured control API."))
    ap.add_argument("--config", default="/etc/seasonalweather/config.yaml")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)

    try:
        asyncio.run(
            run_api_server(
                config_path=args.config,
                host=args.host,
                port=args.port,
            )
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
