"""Exec-friendly controller and worker health commands."""

from __future__ import annotations

import argparse
import http.client
import json
from pathlib import Path
from urllib.parse import urlsplit

from .health_records import health_path, read_health


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="seasonalweather health")
    parser.add_argument("role", choices=("controller", "worker"))
    parser.add_argument("--mode", choices=("liveness", "readiness"), default="readiness")
    parser.add_argument("--url", default="http://127.0.0.1:9080")
    parser.add_argument("--file", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=2.0)
    return parser


def _controller_request(
    args: argparse.Namespace,
) -> tuple[type[http.client.HTTPConnection], str, str, int | None] | None:
    parsed = urlsplit(args.url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        return None
    endpoint = "/healthz" if args.mode == "liveness" else "/readyz"
    request_target = parsed.path.rstrip("/") + endpoint
    if parsed.query:
        request_target += f"?{parsed.query}"
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    return connection_type, parsed.hostname, request_target, parsed.port


def _fetch_controller_health(
    connection_type: type[http.client.HTTPConnection],
    hostname: str,
    request_target: str,
    port: int | None,
    timeout: float,
) -> tuple[int, object] | None:
    connection: http.client.HTTPConnection | http.client.HTTPSConnection | None = None
    try:
        connection = connection_type(
            hostname,
            port,
            timeout=max(0.1, min(timeout, 5.0)),
        )
        connection.request("GET", request_target)
        response = connection.getresponse()
        body = json.loads(response.read(16_384).decode("utf-8"))
        return response.status, body
    except (OSError, TimeoutError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    finally:
        if connection is not None:
            connection.close()


def _controller_health(args: argparse.Namespace) -> int:
    request = _controller_request(args)
    if request is None:
        return 1
    response = _fetch_controller_health(*request, timeout=args.timeout)
    if response is None:
        return 1
    status, body = response
    if status != 200 or not isinstance(body, dict):
        return 1
    if args.mode == "readiness" and body.get("ready") is not True:
        return 1
    return 0


def _worker_health(args: argparse.Namespace) -> int:
    healthy, reason = read_health(health_path(str(args.file) if args.file is not None else None))
    if healthy:
        return 0
    if args.mode == "liveness" and reason not in {
        "health_record_unavailable",
        "health_record_invalid",
        "health_record_oversized",
        "health_record_stale",
        "worker_stopped",
        "worker_failed",
    }:
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.role == "controller":
        return _controller_health(args)
    return _worker_health(args)


if __name__ == "__main__":
    raise SystemExit(main())
