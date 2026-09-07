"""Bounded, cancellable CPU matching outside the API event loop."""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from substar_core.process_command import python_script_command

_slots = asyncio.Semaphore(2)


async def match_reference(payload: bytes, filename: str, units: list[dict[str, Any]],
                          language: str, request: Any) -> dict[str, Any]:
    async with _slots:
        process = await asyncio.create_subprocess_exec(
            *python_script_command("scripts/run_reference_match.py"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=Path(__file__).resolve().parents[3],
        )
        content = json.dumps({"payload": base64.b64encode(payload).decode("ascii"),
                              "filename": filename, "units": units, "language": language}).encode("utf-8")
        communication = asyncio.create_task(process.communicate(content))
        try:
            while not communication.done():
                done, _ = await asyncio.wait({communication}, timeout=0.2)
                if not done and await request.is_disconnected():
                    raise asyncio.CancelledError("reference request disconnected")
            stdout, stderr = await communication
            if process.returncode:
                raise ValueError(stderr.decode("utf-8", errors="replace")[-1500:])
            return json.loads(stdout)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if not communication.done():
                communication.cancel()
                await asyncio.gather(communication, return_exceptions=True)
