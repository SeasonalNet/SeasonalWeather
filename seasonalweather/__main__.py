from __future__ import annotations

import sys
from collections.abc import Callable
from typing import cast


def _main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "diagnostics":
        from .diagnostics.cli import main

        return main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        from importlib import import_module

        worker_main = cast(Callable[[list[str]], int], import_module("seasonalweather.worker.cli").main)
        return worker_main(sys.argv[2:])
    from .main import main

    return main()


if __name__ == "__main__":
    raise SystemExit(_main())
