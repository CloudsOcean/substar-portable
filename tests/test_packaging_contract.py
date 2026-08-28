from __future__ import annotations

import struct
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class PackagingContractTests(unittest.TestCase):
    def test_release_uses_a_transparent_source_runtime_layout(self) -> None:
        script = read_text("scripts/build-windows.ps1")
        self.assertIn("transparent-source-runtime", script)
        self.assertIn("runtime\\python\\python.exe", script)
        self.assertIn("--basetemp $TestBaseTemp", script)
        self.assertIn("build\\pytest-release", script)
        self.assertNotIn("PyInstaller", script)
        self.assertNotIn("_internal", script)

    def test_build_copies_visible_product_roots_and_explicit_workers(self) -> None:
        script = read_text("scripts/build-windows.ps1")
        for resource in ("substar_core", "web", "prompts", "schemas", "runtime\\python", "runtime\\ffmpeg"):
            self.assertIn(f"'{resource}'", script)
        self.assertIn("run_transcription_worker.py", script)
        self.assertIn("run_segmentation_worker.py", script)
        self.assertIn("run_flash_map_pro_editor.py", script)
        self.assertIn("run_global_planner_ab.py", script)
        self.assertIn("run_production_translation.py", script)
        self.assertIn("run_editor_model_request.py", script)
        self.assertNotIn("Copy-RequiredDirectory 'tests'", script)

    def test_release_manifest_contains_no_projects_or_retired_capability(self) -> None:
        manifest = read_text("portable_manifest.json")
        self.assertIn('"version": "1.0.9"', manifest)
        self.assertIn('"package_layout": "transparent-source-runtime"', manifest)
        self.assertIn('"reference-script-v1"', manifest)
        self.assertIn('"advanced-ai-v1"', manifest)
        self.assertNotIn('"public_projects"', manifest)
        self.assertNotIn("legacy", manifest.lower())

    def test_build_replaces_placeholder_with_the_exact_source_commit(self) -> None:
        script = read_text("scripts/build-windows.ps1")
        self.assertIn("$bundleManifest.source_commit = $sourceCommit", script)
        self.assertIn("$env:GITHUB_SHA", script)

    def test_release_bundle_requires_ffmpeg_and_ffprobe(self) -> None:
        build_script = read_text("scripts/build-windows.ps1")
        self.assertIn("runtime\\ffmpeg\\bin", build_script)
        self.assertIn("'ffmpeg.exe', 'ffprobe.exe'", build_script)

    def test_github_builds_materialize_the_ignored_transparent_runtime(self) -> None:
        for workflow_path in (
            ".github/workflows/preview-windows.yml",
            ".github/workflows/release-windows.yml",
        ):
            workflow = read_text(workflow_path)
            self.assertIn("Prepare transparent portable runtime", workflow)
            self.assertIn("$env:pythonLocation", workflow)
            self.assertIn("runtime\\python", workflow)
            self.assertIn("runtime\\ffmpeg\\bin", workflow)
            self.assertIn("'ffmpeg.exe', 'ffprobe.exe'", workflow)
            self.assertIn("python -m pip install -r requirements-test.txt", workflow)
            self.assertIn("python -m pytest tests -q", workflow)
            self.assertIn("node --test tests\\*.test.js", workflow)

    def test_launcher_and_workers_use_one_python_script_command(self) -> None:
        launcher = read_text("launcher.py")
        app_source = read_text("app.py")
        commands = read_text("substar_core/process_command.py")
        self.assertIn('"--smoke-import"', launcher)
        self.assertIn('PROMPTS_DIR = PROJECT_ROOT / "prompts"', app_source)
        self.assertIn("backend_command()", launcher)
        self.assertNotIn('"--backend"', launcher)
        self.assertNotIn("WORKER_FLAG", launcher)
        self.assertNotIn("IS_FROZEN", commands)
        self.assertIn("[sys.executable, str(PROJECT_ROOT / relative)", commands)

    def test_icon_contains_all_supported_windows_sizes(self) -> None:
        icon = (ROOT / "assets" / "substar.ico").read_bytes()
        self.assertEqual(struct.unpack("<HHH", icon[:6]), (0, 1, 7))

    def test_generated_outputs_and_private_state_are_ignored(self) -> None:
        ignored = set(read_text(".gitignore").splitlines())
        for path in ("/runtime/", "/data/", "/build/", "/dist/", "__pycache__/"):
            self.assertIn(path, ignored)

    def test_split_page_does_not_expose_legacy_jianying_srt_upload(self) -> None:
        html = read_text("web/split.html")
        script = read_text("web/split.js")
        for legacy_id in ("jianyingMode", "modeSwitch", "srtRow", "srtInput", "chooseSrtButton"):
            self.assertNotIn(legacy_id, html)
            self.assertNotIn(legacy_id, script)

    def test_split_submission_uses_a_stable_idempotency_key(self) -> None:
        script = read_text("web/split.js")
        self.assertIn("submissionKey: \"\"", script)
        self.assertIn("state.submissionKey = crypto.randomUUID()", script)
        self.assertIn('headers: {"Idempotency-Key": state.submissionKey}', script)

    def test_split_page_uses_canonical_product_labels(self) -> None:
        html = read_text("web/split.html")
        script = read_text("web/split.js")
        application = read_text("app.py")
        for retired_label in (
            "A 原始切分模式",
            "启用后使用 T1mix",
            "切分 Max",
        ):
            self.assertNotIn(retired_label, html)
            self.assertNotIn(retired_label, script)
            self.assertNotIn(retired_label, application)

    def test_breaking_release_does_not_ship_legacy_project_adapters(self) -> None:
        for removed in (
            "substar_core/manual_relay.py",
            "substar_core/production_profiles.py",
            "substar_core/storage/migration.py",
        ):
            self.assertFalse((ROOT / removed).exists(), removed)
        self.assertNotIn('@app.post("/api/jobs")', read_text("app.py"))
        self.assertNotIn('@router.get("/relay"', read_text("substar_core/web_routes.py"))

    def test_release_dependencies_are_pinned_and_release_documents_exist(self) -> None:
        requirements = [
            line.strip()
            for line in read_text("requirements-release.txt").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertTrue(requirements)
        self.assertTrue(all("==" in line for line in requirements))
        test_requirements = [
            line.strip()
            for line in read_text("requirements-test.txt").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertTrue(test_requirements)
        self.assertTrue(all("==" in line for line in test_requirements))
        for document in (
            "README.md",
            "CHANGELOG.md",
            "SECURITY.md",
            "PRIVACY.md",
            "THIRD_PARTY_NOTICES.md",
            "LICENSE",
            "docs/architecture/system-map.json",
            "docs/architecture/system-map.md",
            "docs/architecture/system-map.mmd",
            "docs/architecture/beta-module-map.md",
            "docs/architecture/portable-layout.md",
        ):
            self.assertTrue((ROOT / document).is_file(), document)

    def test_release_script_enforces_the_portable_layout_after_smoke(self) -> None:
        script = read_text("scripts/build-windows.ps1")
        self.assertIn("$AllowedTopLevel", script)
        self.assertIn("Transparent bundle contains user data after smoke cleanup", script)
        self.assertIn("scripts/system_map.py --check", script)
        self.assertIn("docs\\architecture", script)
        self.assertIn("--validate-material-only", script)
        self.assertIn("segmentation_material_v1.json", script)
        self.assertIn("Production file changed during packaging", script)
        self.assertIn("release-verification.json", script)
        self.assertIn("real_video_e2e", script)
        self.assertIn("unicode_space_path", script)

    def test_retired_runtime_modules_are_deleted(self) -> None:
        for removed in (
            "substar_core/full_pipeline.py",
            "substar_core/pipeline.py",
            "substar_core/qwen_backend.py",
            "substar_core/split_bundle.py",
            "substar_core/translation_input_v2.py",
        ):
            self.assertFalse((ROOT / removed).exists(), removed)

    def test_credentials_are_portable_with_the_complete_data_root(self) -> None:
        security = read_text("substar_core/security.py")
        config = read_text("substar_core/config.py")
        self.assertIn("PORTABLE_PREFIX", security)
        self.assertIn("AESGCM", security)
        self.assertIn('"credentials.enc"', config)
        self.assertIn('"credentials.dpapi"', config)


if __name__ == "__main__":
    unittest.main()
