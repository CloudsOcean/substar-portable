"""Portable updates from the project's GitHub releases, staged outside program files."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import uuid
import zipfile
from pathlib import Path, PurePosixPath

import requests
from substar_core.artifacts import atomic_write_json
from substar_core.config import APP_DATA_DIR, INSTALL_ROOT

REPOSITORY = "CloudsOcean/substar-portable"
RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
ROOTS = frozenset({"app.py","launcher.py","substar_core","web","prompts","schemas","scripts","assets",
    "runtime","docs","portable_manifest.json","requirements-release.txt","release-verification.json",
    "启动_Substar.cmd","停止_Substar.cmd","只检测环境.cmd","便携版说明.txt","README.md","CHANGELOG.md",
    "SECURITY.md","PRIVACY.md","THIRD_PARTY_NOTICES.md","LICENSE"})
UPDATE_ROOT = APP_DATA_DIR / "updates"
_lock = threading.RLock()
_state = {"status":"idle"}


def version_tuple(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(value))
    if not match: raise ValueError("仅支持正式版版本号")
    return tuple(map(int,match.groups()))


def current_version():
    return json.loads((INSTALL_ROOT / "portable_manifest.json").read_text(encoding="utf-8"))["version"]


def update_status():
    with _lock:
        result = dict(_state)
    result.update(current_version=current_version(), supported=os.name == "nt" and
                  (INSTALL_ROOT / "runtime/python/python.exe").is_file() and not (INSTALL_ROOT / ".git").exists())
    outcome = UPDATE_ROOT / "outcome.json"
    if outcome.exists():
        try: result["last_result"] = json.loads(outcome.read_text(encoding="utf-8-sig"))
        except (ValueError,OSError): pass
    return result


def check_update():
    response = requests.get(RELEASES_URL, headers={"Accept":"application/vnd.github+json"},timeout=20)
    response.raise_for_status()
    release = response.json()
    version = release.get("tag_name","")
    if release.get("draft") or release.get("prerelease"):
        raise ValueError("更新源未提供正式版")
    available = version_tuple(version) > version_tuple(current_version())
    name = f"Substar-Windows-x64-{version}.zip"
    asset = next((a for a in release.get("assets",[]) if a.get("name") == name),None)
    if available and asset is None: raise ValueError("新版本尚未上传完整 Windows 更新包")
    return {"version":version,"available":available,"notes":str(release.get("body") or ""),"asset":asset}


def validate_archive(archive: Path, destination: Path, expected_version: str):
    """Reject traversal, device names, symlinks and user-data payloads before extracting."""
    with zipfile.ZipFile(archive) as bundle:
        seen, total = set(), 0
        for info in bundle.infolist():
            name = info.filename.replace("\\","/")
            parts = name.rstrip("/").split("/")
            if len(parts) < 2 and parts == ["Substar"] and info.is_dir(): continue
            if len(parts) < 2 or parts[0] != "Substar" or parts[1] not in ROOTS:
                raise ValueError("更新包包含未允许的文件或用户数据")
            if any(p in {"",".",".."} or ":" in p or p.endswith((" ",".")) or
                   re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)",p,re.I) for p in parts):
                raise ValueError("更新包路径无效")
            if stat.S_ISLNK(info.external_attr >> 16): raise ValueError("更新包不能包含符号链接")
            normalized = name.rstrip("/").casefold()
            if normalized in seen: raise ValueError("更新包包含重复路径")
            seen.add(normalized)
            total += info.file_size
            if total > 8 * 1024**3: raise ValueError("更新包解压大小超过限制")
        bundle.extractall(destination)
    root = destination / "Substar"
    manifest = json.loads((root / "portable_manifest.json").read_text(encoding="utf-8"))
    if version_tuple(manifest["version"]) != version_tuple(expected_version):
        raise ValueError("更新包版本与发布信息不符")
    if manifest.get("package_layout") != "transparent-source-runtime":
        raise ValueError("不支持的更新包布局")
    for required in ("app.py","launcher.py","runtime/python/python.exe","启动_Substar.cmd"):
        if not (root / required).is_file(): raise ValueError(f"更新包缺少 {required}")
    return root


def download_update():
    with _lock:
        if _state["status"] in {"downloading","ready","installing"}: return update_status()
        _state.clear(); _state.update(status="downloading", downloaded=0)
    def work():
        try:
            release = check_update()
            if not release["available"]: raise ValueError("当前已经是最新正式版")
            asset = release["asset"]
            digest = str(asset.get("digest") or "")
            if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}",digest):
                raise ValueError("发布包缺少 GitHub SHA-256 校验信息，不能自动安装")
            url = asset.get("browser_download_url","")
            if not url.startswith(f"https://github.com/{REPOSITORY}/releases/download/"):
                raise ValueError("更新包来源无效")
            directory = UPDATE_ROOT / uuid.uuid4().hex
            directory.mkdir(parents=True)
            archive = directory / "package.zip"
            checksum, size = hashlib.sha256(), 0
            with requests.get(url,stream=True,timeout=(20,60)) as response:
                response.raise_for_status()
                with archive.open("wb") as output:
                    for chunk in response.iter_content(1024*1024):
                        size += len(chunk)
                        if size > 4 * 1024**3: raise ValueError("更新包大小超过限制")
                        checksum.update(chunk); output.write(chunk)
                        with _lock: _state.update(downloaded=size,total=asset.get("size",0))
            if checksum.hexdigest().lower() != digest[7:].lower() or size != asset.get("size"):
                raise ValueError("下载校验失败，请重试")
            staged = validate_archive(archive,directory / "unpacked",release["version"])
            with _lock: _state.update(status="ready",version=release["version"],staged=str(staged))
        except Exception as exc:
            with _lock: _state.update(status="failed",error=str(exc))
    threading.Thread(target=work,daemon=True,name="update-download").start()
    return update_status()


def prepare_install(port: int):
    with _lock:
        if not update_status()["supported"]: raise ValueError("仅正式便携版支持应用内安装；源码目录不会被覆盖")
        if _state["status"] != "ready": raise ValueError("请先完成更新包下载")
        plan = {"install":str(INSTALL_ROOT.resolve()),"staged":_state["staged"],"roots":sorted(ROOTS),
                "backup":str(UPDATE_ROOT / ("backup-"+uuid.uuid4().hex)),"port":port,
                "version":_state["version"],"outcome":str(UPDATE_ROOT / "outcome.json")}
        atomic_write_json(UPDATE_ROOT / "pending.json",plan)
        _state["status"] = "installing"


def launch_pending_update():
    """Called by launcher only after releasing the backend job and singleton mutex."""
    pending = UPDATE_ROOT / "pending.json"
    if not pending.exists(): return
    helper = UPDATE_ROOT / "apply-update.ps1"
    helper.write_text((INSTALL_ROOT / "substar_core/update_helper.ps1").read_text(encoding="utf-8"), encoding="utf-8-sig")
    subprocess.Popen(["powershell.exe","-NoProfile","-ExecutionPolicy","Bypass","-File",str(helper),
                      "-PlanPath",str(pending),"-LauncherPid",str(os.getpid())],
                     creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
                     stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
