from __future__ import annotations

import sys


def _main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "diagnostics":
        from .diagnostics.cli import main

        return main(sys.argv[2:])
    from .main import main

    return main()


if __name__ == "__main__":
    raise SystemExit(_main())
