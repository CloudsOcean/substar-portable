"""Signed, baseline-specific file deltas. Unchanged bytes never cross the network."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import stat
import zipfile
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

SCHEMA = "substar.delta.v1"
MAX_BYTES = 8 * 1024**3


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_path(name, roots):
    if not isinstance(name, str) or "\\" in name:
        raise ValueError("差量文件路径无效")
    parts = name.split("/")
    if parts[0] not in roots or any(
        p in {"", ".", ".."} or any(c in p for c in ':<>"|?*') or
        any(ord(c) < 32 for c in p) or p.endswith((" ", ".")) or
        re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", p, re.I)
        for p in parts
    ):
        raise ValueError("差量包包含未允许的文件路径")
    return name


def checked_path(root, name, roots):
    safe_path(name, roots)
    root = Path(root).resolve()
    path = root / name
    # Reject junctions as well as symlinks, including ancestors.
    if not path.resolve().is_relative_to(root):
        raise ValueError("更新路径指向安装目录外")
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink() or (parent.exists() and getattr(parent.stat(), "st_file_attributes", 0) & 1024):
            raise ValueError("更新路径不能包含链接")
    return path


def inventory(root, roots):
    result = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_symlink():
            raise ValueError("发布目录不能包含链接")
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        name = path.relative_to(root).as_posix()
        checked_path(root, name, roots)
        result[name] = {"sha256": sha256(path), "size": path.stat().st_size}
    return result


def verify_manifest(envelope, keys, version, repository, roots):
    try:
        payload = envelope["payload"]
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(keys[envelope["key_id"]], validate=True))
        key.verify(base64.b64decode(envelope["signature"], validate=True), canonical(payload))
    except (KeyError, TypeError, ValueError, InvalidSignature) as exc:
        raise ValueError("增量更新清单签名无效或签名密钥尚未受信任") from exc
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA or payload.get("version") != version or payload.get("repository") != repository:
        raise ValueError("增量清单与发布版本不符")
    files = payload.get("files")
    if not isinstance(files, dict) or not files or len(files) > 100000:
        raise ValueError("增量清单文件列表无效")
    seen, total = set(), 0
    for name, info in files.items():
        safe_path(name, roots)
        folded = name.casefold()
        if folded in seen or any(parent in seen for parent in _parents(folded)):
            raise ValueError("增量清单文件路径冲突")
        seen.add(folded)
        if not isinstance(info, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(info.get("sha256", ""))):
            raise ValueError("增量清单文件校验值无效")
        size = info.get("size")
        if type(size) is not int or size < 0:
            raise ValueError("增量清单文件大小无效")
        total += size
    if any(parent in seen for name in seen for parent in _parents(name)) or total > MAX_BYTES:
        raise ValueError("增量清单路径或总大小无效")
    for required in ("app.py", "launcher.py", "portable_manifest.json", "runtime/python/python.exe", "启动_Substar.cmd"):
        if required not in files:
            raise ValueError("增量清单缺少必需文件")
    patches = payload.get("patches")
    if not isinstance(patches, list) or not 0 < len(patches) <= 100:
        raise ValueError("增量清单缺少有效基线")
    baselines = set()
    for patch in patches:
        if not isinstance(patch, dict):
            raise ValueError("差量包信息无效")
        baseline = patch.get("from_version")
        if not isinstance(baseline, str) or not re.fullmatch(r"v\d+\.\d+\.\d+", baseline) or baseline in baselines:
            raise ValueError("差量包基线无效或重复")
        baselines.add(baseline)
        if patch.get("asset") != f"Substar-Windows-x64-{baseline}-to-{version}.delta.zip":
            raise ValueError("差量包名称与版本不符")
        changed = patch.get("changed")
        if not isinstance(changed, list) or not all(isinstance(name,str) for name in changed) or len(set(changed)) != len(changed) or not set(changed) <= files.keys():
            raise ValueError("差量包文件列表无效")
        if not re.fullmatch(r"[0-9a-f]{64}", str(patch.get("sha256", ""))) or type(patch.get("size")) is not int or not 0 < patch["size"] <= MAX_BYTES:
            raise ValueError("差量包校验信息无效")
    return payload


def _parents(name):
    parts = name.split("/")
    return ("/".join(parts[:i]) for i in range(1, len(parts)))


def baseline_matches(root, payload, patch, roots):
    changed = set(patch["changed"])
    for name, info in payload["files"].items():
        if name in changed:
            continue
        path = checked_path(root, name, roots)
        if not path.is_file() or path.stat().st_size != info["size"] or sha256(path) != info["sha256"]:
            return False
    return True


def stage_delta(archive, root, destination, payload, patch, roots):
    """Build a complete verified staging tree using local unchanged files and a small ZIP."""
    destination = Path(destination)
    if destination.exists():
        raise ValueError("增量暂存目录已存在")
    changed = set(patch["changed"])
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        names = [m.filename for m in members]
        if len(set(names)) != len(names) or set(names) != changed:
            raise ValueError("差量包文件与签名清单不符")
        for member in members:
            if member.is_dir() or stat.S_ISLNK(member.external_attr >> 16) or member.file_size != payload["files"][member.filename]["size"]:
                raise ValueError("差量包文件属性无效")
        for name, info in payload["files"].items():
            target = checked_path(destination, name, roots)
            target.parent.mkdir(parents=True, exist_ok=True)
            if name in changed:
                with bundle.open(name) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
            else:
                shutil.copyfile(checked_path(root, name, roots), target)
            if target.stat().st_size != info["size"] or sha256(target) != info["sha256"]:
                raise ValueError("增量文件校验失败，请重新检查更新")
    manifest = json.loads((destination / "portable_manifest.json").read_text(encoding="utf-8-sig"))
    if "v" + manifest["version"].removeprefix("v") != payload["version"] or manifest.get("package_layout") != "transparent-source-runtime":
        raise ValueError("增量暂存版本不符")
    return destination
