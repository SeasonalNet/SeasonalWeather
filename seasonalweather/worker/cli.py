"""Command-line entrypoint for a bounded outbound worker process."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import os
import signal
import uuid
from pathlib import Path

from ..build_metadata import current_build_info
from ..build_metadata.compatibility import BuildCompatibilityError, ensure_runtime_compatibility
from ..diagnostics.bindings import FOUNDATION_CODES
from ..logging_config import setup_logging
from ..secret_files import SecretFileError, merge_secret_files
from ..swwp.worker import WorkerSession
from .handlers import HandlerRegistry
from .profiles import WorkerProfile, registration_for_profile, worker_id_from_environment
from .runtime import WorkerRuntime
from .transport import WebSocketWorkerTransport


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seasonalweather worker",
        description="Run one capability-specific outbound SeasonalWeather worker.",
    )
    parser.add_argument(
        "--controller-url",
        default=os.environ.get("SEASONALWEATHER_CONTROLLER_URL", ""),
        help="controller SWWP WebSocket URL (or SEASONALWEATHER_CONTROLLER_URL)",
    )
    parser.add_argument(
        "--worker-id",
        default=os.environ.get("SEASONALWEATHER_WORKER_ID", ""),
        help="stable worker identity (or SEASONALWEATHER_WORKER_ID)",
    )
    parser.add_argument(
        "--worker-instance-id",
        default=os.environ.get("SEASONALWEATHER_WORKER_INSTANCE_ID"),
    )
    parser.add_argument(
        "--worker-epoch",
        type=int,
        default=int(os.environ.get("SEASONALWEATHER_WORKER_EPOCH", "1")),
    )
    parser.add_argument(
        "--slots",
        type=int,
        default=int(os.environ.get("SEASONALWEATHER_WORKER_SLOTS", "1")),
    )
    parser.add_argument(
        "--profile",
        choices=tuple(profile.value for profile in WorkerProfile),
        default=os.environ.get("SEASONALWEATHER_WORKER_PROFILE", WorkerProfile.ROUTINE.value),
    )
    parser.add_argument(
        "--health-file",
        default=os.environ.get("SEASONALWEATHER_WORKER_HEALTH_FILE"),
        help="local bounded health record path (or SEASONALWEATHER_WORKER_HEALTH_FILE)",
    )
    parser.add_argument(
        "--input-root",
        default=os.environ.get(
            "SEASONALWEATHER_WORKER_INPUT_ROOT",
            "/var/lib/seasonalweather/artifacts/worker-artifacts/staging/controller-inputs",
        ),
        help="shared controller input descriptor directory",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("SEASONALWEATHER_WORKER_TOKEN", ""),
        help="SWWP bearer token (or SEASONALWEATHER_WORKER_TOKEN)",
    )
    return parser


def _worker_token() -> str:
    """Resolve the worker credential from compatibility env or its secret file."""

    token = os.environ.get("SEASONALWEATHER_WORKER_TOKEN", "")
    if token:
        return token
    secret_directory = os.environ.get("SEASONALWEATHER_SECRET_DIR", "/run/secrets")
    try:
        merged = merge_secret_files(
            os.environ,
            config_value={"paths": {"secret_dir": secret_directory}},
        )
    except SecretFileError as exc:
        raise SystemExit(f"seasonalweather worker: invalid secret file: {exc}") from None
    return merged.get("SEASONAL_WORKER_TOKEN", "")


async def _run_worker(runtime: WorkerRuntime) -> None:
    loop = asyncio.get_running_loop()
    stop_tasks: set[asyncio.Task[None]] = set()

    def request_stop() -> None:
        task = asyncio.create_task(runtime.stop(), name="seasonalweather-worker-stop")
        stop_tasks.add(task)
        task.add_done_callback(stop_tasks.discard)

    installed: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_stop)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(sig)
    try:
        await runtime.run()
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
        if stop_tasks:
            await asyncio.gather(*stop_tasks, return_exceptions=True)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    profile = WorkerProfile(args.profile)
    if not args.controller_url:
        raise SystemExit("seasonalweather worker: --controller-url is required")
    build_info = current_build_info()
    try:
        ensure_runtime_compatibility(
            build_info,
            role="worker",
            expected_profile=profile.value,
        )
    except BuildCompatibilityError as exc:
        logging.getLogger("seasonalweather.build").critical(
            "Build identity is incompatible with the worker runtime.",
            extra={
                "event": "build_compatibility_rejected",
                "code": FOUNDATION_CODES["build.compatibility_rejected"],
                "reason": str(exc),
            },
        )
        raise SystemExit(f"seasonalweather worker: incompatible build: {exc}") from None
    worker_id = args.worker_id or worker_id_from_environment(profile)
    handlers = HandlerRegistry.for_profile(
        profile.value,
        worker_id=worker_id,
        input_root=Path(args.input_root),
    )
    registration = registration_for_profile(
        profile,
        worker_id=worker_id,
        worker_instance_id=args.worker_instance_id,
        worker_epoch=args.worker_epoch,
        slots=args.slots,
        handler_ready=handlers.ready,
        handler_ready_job_types=handlers.executable_job_types,
        handler_ready_capabilities=frozenset(
            {"tts.synthesis.v1", "audio.alert_artifact.v1"} if handlers.executable_job_types else set()
        ),
    )
    session = WorkerSession(
        registration=registration,
        id_factory=lambda prefix: f"{prefix}-{uuid.uuid4().hex[:20]}",
        clock=lambda: dt.datetime.now(dt.UTC),
        assignment_acceptor=handlers.supports,
    )
    runtime = WorkerRuntime(
        session,
        handlers,
        WebSocketWorkerTransport(args.controller_url, token=args.token or _worker_token()),
        health_file=args.health_file,
        image_profile=profile.value,
        reconnect=True,
    )
    setup_logging(
        role="worker",
        instance_id=registration.worker_instance_id,
        build_info=build_info,
    )
    asyncio.run(_run_worker(runtime))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
