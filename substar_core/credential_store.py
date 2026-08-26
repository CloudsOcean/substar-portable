from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .artifacts import atomic_write_text
from .security import protect_text, unprotect_text


ASR_QWEN = "asr_qwen"
ASR_GENERIC = "asr_generic"
SEGMENT_DEEPSEEK = "segment_deepseek"
TRANSLATE_DEEPSEEK = "translate_deepseek"
ALIGN_DEEPSEEK = "align_deepseek"

CANONICAL_CREDENTIAL_ROLES = frozenset(
    {
        ASR_QWEN,
        ASR_GENERIC,
        SEGMENT_DEEPSEEK,
        TRANSLATE_DEEPSEEK,
        ALIGN_DEEPSEEK,
    }
)


def credential_key_path(envelope_path: Path) -> Path:
    return envelope_path.with_name("credentials.key")

def clean_credential(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) < 8 or any(ord(character) < 32 for character in text):
        return ""
    return text


def canonicalize_credentials(values: Mapping[str, Any]) -> dict[str, str]:
    cleaned = {
        str(role): value
        for role, raw in values.items()
        if (value := clean_credential(raw))
    }
    return {
        role: cleaned[role]
        for role in CANONICAL_CREDENTIAL_ROLES
        if role in cleaned
    }


def read_envelope(
    candidates: Iterable[Path],
) -> tuple[dict[str, str], bool, bool, str | None]:
    found = False
    failed = False
    for candidate in candidates:
        if not candidate.is_file():
            continue
        found = True
        try:
            payload = json.loads(
                unprotect_text(
                    candidate.read_text(encoding="ascii").strip(),
                    key_path=credential_key_path(candidate),
                )
            )
            values = payload.get("credentials", payload) if isinstance(payload, dict) else None
            if not isinstance(values, dict):
                raise ValueError("credential envelope is not an object")
            schema_version = str(payload.get("schema_version", "")) if isinstance(payload, dict) else ""
            return canonicalize_credentials(values), True, False, schema_version or None
        except Exception:
            failed = True
    return {}, found, failed, None


def write_envelope(path: Path, values: Mapping[str, Any]) -> None:
    canonical = canonicalize_credentials(values)
    payload = {
        "schema_version": "substar.credentials.v2",
        "credentials": canonical,
    }
    atomic_write_text(
        path,
        protect_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            key_path=credential_key_path(path),
        ),
        encoding="ascii",
    )


def load_store(
    candidates: Iterable[Path],
) -> dict[str, str]:
    """Load the one canonical credential envelope."""

    unified, found, failed, schema_version = read_envelope(candidates)
    if found and not failed and schema_version == "substar.credentials.v2":
        return unified
    return {}
