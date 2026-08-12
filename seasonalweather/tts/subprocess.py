"""One bounded subprocess policy for local synthesis engines."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from shutil import which

from .cancellation import deadline_expired, explicit_cancellation
from .failures import ProcessFailure


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    output_limited: bool


def resolve_trusted_executable(name: str) -> str:
    """Resolve a handler-owned executable; arbitrary command strings are rejected."""
    if not name or any(ch in name for ch in "\x00\r\n;|&$`"):
        raise ProcessFailure("executable_unavailable", "local executable name is invalid")
    resolved = name if os.path.isabs(name) else which(name)
    if not resolved or not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
        raise ProcessFailure("executable_unavailable", "required local executable is unavailable")
    return resolved


def _bounded_output(chunks: list[bytes], *, limit: int) -> tuple[str, bool]:
    raw = b"".join(chunks)
    limited = len(raw) > limit
    raw = raw[:limit]
    safe = "".join(ch if ch.isprintable() or ch in "\n\t" else "?" for ch in raw.decode("utf-8", "replace"))
    return safe, limited


def _terminate_process_group(process: subprocess.Popen[bytes], *, kill: bool) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL if kill else signal.SIGTERM)
        elif kill:
            process.kill()
        else:
            process.terminate()
    except (ProcessLookupError, OSError):
        pass


def run_bounded(
    argv: list[str],
    *,
    input_bytes: bytes | None,
    deadline: float,
    cancellation: threading.Event | None = None,
    cwd: Path | None = None,
    output_limit: int = 64 * 1024,
    terminate_grace_seconds: float = 0.25,
) -> ProcessResult:
    """Run an explicit argv until its shared absolute deadline or cancellation."""
    _validate_request(argv, input_bytes, deadline, cancellation)
    process = _launch_process(argv, input_bytes, cwd)

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    output_limited = False

    def drain(stream: object, chunks: list[bytes]) -> None:
        nonlocal output_limited
        if stream is None:
            return
        reader = stream  # typing is intentionally narrow at the subprocess boundary
        while True:
            block = reader.read(4096)  # type: ignore[attr-defined]
            if not block:
                return
            current_size = sum(len(item) for item in chunks)
            if current_size < output_limit:
                chunks.append(block[: output_limit - current_size])
            if current_size + len(block) > output_limit:
                output_limited = True

    stdout_thread, stderr_thread = _start_drainers(process, drain, stdout_chunks, stderr_chunks)
    try:
        if input_bytes is not None and process.stdin is not None:
            _start_input_writer(process, input_bytes)
        returncode = _wait_bounded(process, deadline, cancellation, terminate_grace_seconds)
    finally:
        _finish_process(process, stdout_thread, stderr_thread, terminate_grace_seconds)

    stdout, stdout_limited = _bounded_output(stdout_chunks, limit=output_limit)
    stderr, stderr_limited = _bounded_output(stderr_chunks, limit=output_limit)
    output_limited = output_limited or stdout_limited or stderr_limited
    if output_limited:
        raise ProcessFailure("output_limit", "local synthesis process output exceeded its bound")
    if returncode != 0:
        raise ProcessFailure("nonzero_exit", "local synthesis process returned a nonzero status")
    return ProcessResult(returncode or 0, stdout, stderr, False)


def _validate_request(
    argv: list[str], input_bytes: bytes | None, deadline: float, cancellation: threading.Event | None
) -> None:
    if not argv or any("\x00" in item for item in argv):
        raise ProcessFailure("invalid_argv", "local subprocess argv is invalid")
    if input_bytes is not None and len(input_bytes) > 65_536:
        raise ProcessFailure("input_limit", "local synthesis input exceeded its bound")
    if deadline_expired(cancellation) or time.monotonic() >= deadline:
        raise ProcessFailure("timed_out", "local synthesis deadline expired before process start")
    if explicit_cancellation(cancellation):
        raise ProcessFailure("cancelled", "local synthesis was cancelled before process start")


def _launch_process(argv: list[str], input_bytes: bytes | None, cwd: Path | None) -> subprocess.Popen[bytes]:
    environment = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    try:
        return subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd is not None else None,
            env=environment,
            start_new_session=(os.name == "posix"),
        )
    except (OSError, ValueError) as exc:
        raise ProcessFailure("executable_unavailable", "local synthesis process could not start") from exc


def _finish_process(
    process: subprocess.Popen[bytes],
    stdout_thread: threading.Thread,
    stderr_thread: threading.Thread,
    grace_seconds: float,
) -> None:
    if process.poll() is None:
        _terminate_process_group(process, kill=True)
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process, kill=True)
        process.wait()
    stdout_thread.join(timeout=grace_seconds)
    stderr_thread.join(timeout=grace_seconds)
    if process.stdin is not None:
        with suppress(OSError):
            process.stdin.close()


def _start_drainers(
    process: subprocess.Popen[bytes],
    drain: object,
    stdout_chunks: list[bytes],
    stderr_chunks: list[bytes],
) -> tuple[threading.Thread, threading.Thread]:
    stdout_thread = threading.Thread(target=drain, args=(process.stdout, stdout_chunks), daemon=True)  # type: ignore[arg-type]
    stderr_thread = threading.Thread(target=drain, args=(process.stderr, stderr_chunks), daemon=True)  # type: ignore[arg-type]
    stdout_thread.start()
    stderr_thread.start()
    return stdout_thread, stderr_thread


def _start_input_writer(process: subprocess.Popen[bytes], input_bytes: bytes) -> None:
    def write_input() -> None:
        try:
            if process.stdin is not None:
                process.stdin.write(input_bytes)
                process.stdin.close()
        except OSError:
            pass

    threading.Thread(target=write_input, daemon=True).start()


def _wait_bounded(
    process: subprocess.Popen[bytes],
    deadline: float,
    cancellation: threading.Event | None,
    terminate_grace_seconds: float,
) -> int:
    while process.poll() is None:
        if deadline_expired(cancellation) or time.monotonic() >= deadline:
            _stop_and_reap(process, terminate_grace_seconds)
            raise ProcessFailure("timed_out", "local synthesis deadline expired")
        if explicit_cancellation(cancellation):
            _stop_and_reap(process, terminate_grace_seconds)
            raise ProcessFailure("cancelled", "local synthesis was cancelled")
        time.sleep(min(0.02, max(0.001, deadline - time.monotonic())))
    return process.returncode or 0


def _stop_and_reap(process: subprocess.Popen[bytes], grace_seconds: float) -> None:
    _terminate_process_group(process, kill=False)
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process, kill=True)
