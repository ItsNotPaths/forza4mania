"""forzamania — FM4 → TM2020 porter, Tk app entry."""
from __future__ import annotations


def main() -> int:
    from ui.app import run_app
    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
