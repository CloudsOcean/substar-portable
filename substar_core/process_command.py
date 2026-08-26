from __future__ import annotations

import sys
from pathlib import Path

from .config import PROJECT_ROOT


def python_script_command(relative_script: str | Path, *args: object) -> list[str]:
    """Build the one canonical child command used in development and portable builds."""

    relative = Path(relative_script).as_posix()
    rendered_args = [str(value) for value in args]
    return [sys.executable, str(PROJECT_ROOT / relative), *rendered_args]


def backend_command() -> list[str]:
    return python_script_command("app.py")
