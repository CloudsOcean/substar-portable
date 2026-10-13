"""Build signed delta assets from unpacked, clean portable release directories."""
from __future__ import annotations

import argparse
import base64
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from substar_core.update_delta import SCHEMA, canonical, inventory, sha256, verify_manifest
from substar_core.updater import REPOSITORY, ROOTS, version_tuple


def build(target, baselines, output, key, key_id):
    target, output = Path(target), Path(output)
    version = "v" + json.loads((target / "portable_manifest.json").read_text(encoding="utf-8-sig"))["version"].removeprefix("v")
    files = inventory(target, ROOTS)
    payload = {"schema": SCHEMA, "repository": REPOSITORY, "version": version, "files": files, "patches": []}
    output.mkdir(parents=True, exist_ok=True)
    versions = set()
    for baseline in baselines:
        baseline = Path(baseline)
        old_version = "v" + json.loads((baseline / "portable_manifest.json").read_text(encoding="utf-8-sig"))["version"].removeprefix("v")
        if old_version in versions or version_tuple(old_version) >= version_tuple(version):
            raise ValueError("基线版本重复或不低于目标版本")
        versions.add(old_version)
        old_files = inventory(baseline, ROOTS)
        changed = sorted(name for name, info in files.items() if old_files.get(name) != info)
        name = f"Substar-Windows-x64-{old_version}-to-{version}.delta.zip"
        archive = output / name
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
            for item in changed:
                bundle.write(target / item, item)
        payload["patches"].append({"from_version": old_version, "asset": name, "sha256": sha256(archive),
                                   "size": archive.stat().st_size, "changed": changed})
    if not payload["patches"]:
        raise ValueError("至少需要一个已发布版本作为基线")
    envelope = {"key_id": key_id, "payload": payload, "signature": base64.b64encode(key.sign(canonical(payload))).decode("ascii")}
    public = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode("ascii")
    verify_manifest(envelope, {key_id: public}, version, REPOSITORY, ROOTS)
    manifest = output / f"Substar-update-{version}.json"
    manifest.write_bytes(canonical(envelope))
    return manifest, payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True, help="External Ed25519 PEM; never include in a release")
    parser.add_argument("--key-id", required=True)
    args = parser.parse_args()
    key = serialization.load_pem_private_key(args.private_key.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("需要 Ed25519 签名密钥")
    manifest, payload = build(args.target, args.baseline, args.output, key, args.key_id)
    print(manifest)
    for patch in payload["patches"]:
        print(f'{patch["asset"]}: {patch["size"]} bytes, {len(patch["changed"])} changed files')


if __name__ == "__main__":
    main()
