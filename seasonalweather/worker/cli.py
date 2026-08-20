"""Command-line entrypoint for a bounded outbound worker process."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import uuid

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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    profile = WorkerProfile(args.profile)
    if not args.controller_url:
        raise SystemExit("seasonalweather worker: --controller-url is required")
    worker_id = args.worker_id or worker_id_from_environment(profile)
    handlers = HandlerRegistry.for_profile(profile.value)
    registration = registration_for_profile(
        profile,
        worker_id=worker_id,
        worker_instance_id=args.worker_instance_id,
        worker_epoch=args.worker_epoch,
        slots=args.slots,
        handler_ready=handlers.ready,
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
        WebSocketWorkerTransport(args.controller_url),
    )
    asyncio.run(runtime.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
