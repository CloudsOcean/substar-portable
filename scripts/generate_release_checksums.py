from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "SHA256SUMS.json"

ROOT_FILES = {
    "README.md",
    "app.py",
    "launcher.py",
    "portable_manifest.json",
    "requirements-release.txt",
    "requirements.txt",
    "便携版说明.txt",
    "只检测环境.cmd",
    "启动_Substar.cmd",
    "停止_Substar.cmd",
}
PACKAGE_DIRECTORIES = (
    ".github",
    "assets",
    "prompts",
    "schemas",
    "scripts",
    "substar_core",
    "web",
)
IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo", ".tmp", ".swp"}


def included_files() -> list[Path]:
    files = [ROOT / name for name in sorted(ROOT_FILES)]
    for directory in PACKAGE_DIRECTORIES:
        files.extend(path for path in (ROOT / directory).rglob("*") if path.is_file())
    return sorted(
        (
            path
            for path in files
            if path.exists()
            and not any(part in IGNORED_PARTS for part in path.relative_to(ROOT).parts)
            and path.suffix.lower() not in IGNORED_SUFFIXES
        ),
        key=lambda path: path.relative_to(ROOT).as_posix(),
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    checksums = {
        path.relative_to(ROOT).as_posix(): sha256(path)
        for path in included_files()
    }
    OUTPUT.write_text(
        json.dumps(checksums, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(checksums)} checksums to {OUTPUT}")


if __name__ == "__main__":
    main()
