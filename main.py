"""Точка входа Velora Sound."""
from __future__ import annotations

import sys


def main() -> int:
    try:
        from velora.ui.app import run
    except ImportError as exc:
        print("Не удалось импортировать зависимости:", exc, file=sys.stderr)
        print("Установите их: pip install -r requirements.txt", file=sys.stderr)
        return 1
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
