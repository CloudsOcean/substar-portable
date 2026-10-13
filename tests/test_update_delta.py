import base64
import copy
import hashlib
import json
import shutil
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.build_update_delta import build
from substar_core import updater
from substar_core.update_delta import baseline_matches, canonical, inventory, stage_delta, verify_manifest


@pytest.fixture
def release(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    for folder, version in ((old, "2.2.7"), (new, "2.2.8")):
        (folder / "runtime/python").mkdir(parents=True)
        (folder / "runtime/python/python.exe").write_bytes(b"unchanged runtime" * 10000)
        (folder / "launcher.py").write_text("same")
        (folder / "app.py").write_text(version)
        (folder / "启动_Substar.cmd").write_text("same")
        (folder / "portable_manifest.json").write_text(json.dumps({"version": version, "package_layout": "transparent-source-runtime"}))
        (folder / "web").mkdir()
    (old / "web/removed.js").write_text("obsolete")
    (new / "web/new.js").write_text("new")
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    output = tmp_path / "release"
    manifest, payload = build(new, [old], output, key, "test")
    return old, new, output, manifest, payload, key, {"test": public}


def test_delta_contains_only_changes_and_reconstructs_exact_target(release, tmp_path):
    old, new, output, manifest, payload, key, keys = release
    verified = verify_manifest(json.loads(manifest.read_bytes()), keys, "v2.2.8", updater.REPOSITORY, updater.ROOTS)
    patch = payload["patches"][0]
    with zipfile.ZipFile(output / patch["asset"]) as archive:
        assert set(archive.namelist()) == {"app.py", "portable_manifest.json", "web/new.js"}
    (old / "data").mkdir(); (old / "data/project.json").write_text("private")
    staged = stage_delta(output / patch["asset"], old, tmp_path / "staged", verified, patch, updater.ROOTS)
    assert inventory(staged, updater.ROOTS) == inventory(new, updater.ROOTS)
    assert not (staged / "web/removed.js").exists()
    assert (old / "data/project.json").read_text() == "private"
    assert patch["size"] < (old / "runtime/python/python.exe").stat().st_size


@pytest.mark.parametrize("fault", ["signature", "unknown_key", "version", "traversal", "collision"])
def test_reject_invalid_signed_manifest(release, fault):
    *_, manifest, payload, key, keys = release
    envelope = json.loads(manifest.read_bytes())
    if fault == "signature": envelope["payload"]["files"]["app.py"]["sha256"] = "0" * 64
    if fault == "unknown_key": keys = {}
    if fault in {"traversal", "collision"}:
        name = "web/../data/secret" if fault == "traversal" else "APP.py"
        envelope["payload"]["files"][name] = envelope["payload"]["files"]["app.py"]
        envelope["signature"] = base64.b64encode(key.sign(canonical(envelope["payload"]))).decode()
    with pytest.raises(ValueError):
        verify_manifest(envelope, keys, "v2.2.9" if fault == "version" else "v2.2.8", updater.REPOSITORY, updater.ROOTS)


def test_local_change_after_check_is_caught_before_install(release, tmp_path):
    old, _, output, _, payload, *_ = release
    patch = payload["patches"][0]
    assert baseline_matches(old, payload, patch, updater.ROOTS)
    (old / "launcher.py").write_text("tampered")
    assert not baseline_matches(old, payload, patch, updater.ROOTS)
    with pytest.raises(ValueError, match="校验"):
        stage_delta(output / patch["asset"], old, tmp_path / "stage", payload, patch, updater.ROOTS)
    assert (old / "app.py").read_text() == "2.2.7"


def test_delta_rejects_extra_archive_member(release, tmp_path):
    old, _, output, _, payload, *_ = release
    patch = payload["patches"][0]
    with zipfile.ZipFile(output / patch["asset"], "a") as z: z.writestr("../escape", "bad")
    with pytest.raises(ValueError, match="清单"):
        stage_delta(output / patch["asset"], old, tmp_path / "stage", payload, patch, updater.ROOTS)
    assert not (tmp_path / "stage").exists()


def network_fixture(release, tmp_path, monkeypatch):
    old, _, output, manifest, payload, _, keys = release
    key_path = tmp_path / "keys.json"; key_path.write_text(json.dumps(keys))
    monkeypatch.setattr(updater, "KEYS_PATH", key_path)
    monkeypatch.setattr(updater, "INSTALL_ROOT", old)
    monkeypatch.setattr(updater, "UPDATE_ROOT", tmp_path / "updates")
    monkeypatch.setattr(updater, "_state", {"status": "idle"})
    data, assets, urls = {}, [], []
    for path in (manifest, output / payload["patches"][0]["asset"]):
        content = path.read_bytes()
        url = f"https://github.com/{updater.REPOSITORY}/releases/download/v2.2.8/{path.name}"
        assets.append({"name": path.name, "size": len(content), "digest": "sha256:" + hashlib.sha256(content).hexdigest(), "browser_download_url": url})
        data[url] = content
    assets.append({"name": "Substar-Windows-x64-v2.2.8.zip", "size": 999999, "browser_download_url": "FULL_DOWNLOAD_MUST_NOT_HAPPEN"})
    class Response:
        def __init__(self, url): self.url = url
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def json(self): return {"tag_name": "v2.2.8", "assets": assets}
        def iter_content(self, size): yield data[self.url]
    def get(url, **kwargs):
        urls.append(url)
        assert url == updater.RELEASES_URL or url in data, "Unexpected full-package download"
        return Response(url)
    monkeypatch.setattr(updater.requests, "get", get)
    class ImmediateThread:
        def __init__(self, target, **kwargs): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(updater.threading, "Thread", ImmediateThread)
    return urls


def test_download_uses_only_manifest_and_delta(release, tmp_path, monkeypatch):
    urls = network_fixture(release, tmp_path, monkeypatch)
    result = updater.download_update(expected_mode="delta")
    assert result["status"] == "ready", result
    assert result["mode"] == "delta"
    assert len(urls) == 3  # release metadata, manifest, delta; never the full ZIP
    assert result["downloaded"] == release[4]["patches"][0]["size"]


def test_modified_baseline_requires_explicit_full_download(release, tmp_path, monkeypatch):
    urls = network_fixture(release, tmp_path, monkeypatch)
    (release[0] / "launcher.py").write_text("local modification")
    offer = updater.check_update()
    assert offer["mode"] == "full" and "基线" in offer["reason"]
    result = updater.download_update(expected_mode="delta")
    assert result["status"] == "failed" and "重新检查" in result["error"]
    assert all("delta.zip" not in url for url in urls)
