from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path


APPLICATION_ROOT = Path(__file__).resolve().parents[1]
if str(APPLICATION_ROOT) not in sys.path:
    sys.path.insert(0, str(APPLICATION_ROOT))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Exercise the release upload through the canonical transcription and "
            "segmentation task graph using real media and deterministic provider stubs."
        )
    )
    parser.add_argument("media", type=Path)
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    media = arguments.media.resolve()
    if not media.is_file():
        raise SystemExit(f"media does not exist: {media}")

    # These must be frozen before importing application modules because their
    # storage roots and edition policy are resolved at import time.
    with tempfile.TemporaryDirectory(prefix="substar-release-acceptance-") as temporary:
        acceptance_root = Path(temporary).resolve()
        os.environ["SUBSTAR_DATA_ROOT"] = str(acceptance_root / "data")
        os.environ["SUBSTAR_EDITION"] = "standard"
        os.environ["SUBSTAR_MOCK_QWEN"] = "1"
        os.environ["SUBSTAR_MOCK_SEGMENTATION"] = "1"
        os.environ["SUBSTAR_OPEN_BROWSER"] = "0"

        import app as application
        from starlette.datastructures import UploadFile

        from substar_core.storage import ProjectStore
        from substar_core.runtime import (
            RuntimeStore,
            TaskRegistry,
            TaskScheduler,
            TaskService,
        )
        from substar_core.transcription import (
            RECOGNITION_EVIDENCE_SCHEMA,
            build_transcription_handler,
            validate_recognition_evidence,
        )
        from substar_core.segmentation import build_segmentation_handler

        projects_root = application.DATA_ROOT / "projects"
        service = TaskService(
            RuntimeStore(acceptance_root / "runtime.sqlite3"),
            "release-acceptance",
        )
        registry = TaskRegistry()
        registry.register(
            build_transcription_handler(projects_root, application.PROJECT_ROOT)
        )
        registry.register(
            build_segmentation_handler(projects_root, application.PROJECT_ROOT)
        )
        scheduler = TaskScheduler(
            service,
            registry,
            acceptance_root / "task-runtime",
            poll_seconds=0.02,
            heartbeat_seconds=0.2,
            resource_limits={
                "worker": 1,
                "local_gpu": 1,
                "media_cpu": 1,
                "provider_io": 1,
                "project_write": 1,
                "download_io": 1,
            },
        )
        application.app.state.task_store = service.store
        application.app.state.task_service = service
        application.app.state.task_registry = registry
        application.app.state.task_scheduler = scheduler
        scheduler.start()

        source_sha256 = _sha256(media)
        job_id = ""
        try:
            source = media.open("rb")
            response = asyncio.run(
                application.create_workbench_split_job(
                    mode="asr",
                    media=UploadFile(source, filename=media.name),
                    srt=None,
                    reference_document=None,
                    settings_json=json.dumps(
                        {
                            "recognition_profile_id": "faster_whisper_native",
                            "language": "Auto",
                            "segmentation_enabled": True,
                            "translation_enabled": False,
                            "calibration_enabled": False,
                            "review_enabled": False,
                        }
                    ),
                    idempotency_key="release-real-media",
                )
            )
            created = json.loads(response.body.decode("utf-8"))
            job_id = str(created["id"])
            replay_source = media.open("rb")
            replay = asyncio.run(
                application.create_workbench_split_job(
                    mode="asr",
                    media=UploadFile(replay_source, filename=media.name),
                    srt=None,
                    reference_document=None,
                    settings_json=json.dumps(
                        {
                            "recognition_profile_id": "faster_whisper_native",
                            "language": "Auto",
                            "segmentation_enabled": True,
                            "translation_enabled": False,
                            "calibration_enabled": False,
                            "review_enabled": False,
                        }
                    ),
                    idempotency_key="release-real-media",
                )
            )
            replayed = json.loads(replay.body.decode("utf-8"))
            if replayed["id"] != job_id:
                raise RuntimeError("idempotent replay created a second project")
            deadline = time.monotonic() + arguments.timeout
            while time.monotonic() < deadline:
                with application.JOBS_LOCK:
                    job = application.JOBS[job_id]
                application._refresh_canonical_job_projection(job)
                with application.JOBS_LOCK:
                    current = job.public()
                if current["status"] in {
                    "awaiting_edit",
                    "completed",
                    "failed",
                    "interrupted",
                    "cancelled",
                }:
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError(f"compatibility job did not finish: {current}")

            if current["status"] != "awaiting_edit":
                task_debug = {}
                for label, candidate_id in (
                    ("transcription", current.get("transcription_task_id")),
                    ("segmentation", current.get("segmentation_task_id")),
                ):
                    if candidate_id:
                        task_debug[label] = service.get_task(str(candidate_id))
                stderr_debug = {
                    str(path.relative_to(acceptance_root)): path.read_text(
                        encoding="utf-8", errors="replace"
                    )
                    for path in (acceptance_root / "task-runtime").rglob("stderr.log")
                }
                raise RuntimeError(
                    f"compatibility job failed: {current}; "
                    f"tasks={task_debug}; stderr={stderr_debug}"
                )
            task_id = str(current["transcription_task_id"])
            task = service.get_task(task_id)
            if task["state"] != "succeeded":
                raise RuntimeError(f"canonical task did not succeed: {task}")
            if task_id == job_id:
                raise RuntimeError("task identity was incorrectly reused as project identity")
            segmentation_task_id = str(current["segmentation_task_id"])
            segmentation_task = service.get_task(segmentation_task_id)
            if segmentation_task["state"] != "succeeded":
                raise RuntimeError(
                    f"canonical segmentation did not succeed: {segmentation_task}"
                )
            if segmentation_task["parent_task_id"] != task_id:
                raise RuntimeError("segmentation task is detached from transcription")

            project = projects_root / job_id
            evidence = validate_recognition_evidence(
                json.loads(
                    (project / "recognition_evidence.json").read_text(
                        encoding="utf-8"
                    )
                )
            )
            if evidence["schema_version"] != RECOGNITION_EVIDENCE_SCHEMA:
                raise RuntimeError("recognition evidence schema changed")
            if evidence["media"]["sha256"] != source_sha256:
                raise RuntimeError("recognition evidence is bound to different media")

            store = ProjectStore.open(project / "project")
            latest = store.load_latest()
            if latest is None:
                raise RuntimeError("editor revision was not materialized")
            if latest.revision_number != 1:
                raise RuntimeError("initial segmentation created multiple revisions")
            operations = {change.operation for change in latest.document.changes}
            if "build_from_segmentation" not in operations:
                raise RuntimeError("editor document lacks canonical segmentation lineage")
            if not (project / "audio_16k_mono.wav").is_file():
                raise RuntimeError("waveform audio was not materialized")

            segmentation_artifacts = service.list_artifacts(segmentation_task_id)
            if len(segmentation_artifacts) != 6:
                raise RuntimeError(
                    f"segmentation artifact contract changed: {segmentation_artifacts}"
                )
            for required in (
                "segmentation_candidate.json",
                "segmentation_validation.json",
                "segmentation_manifest.json",
                "editor_revision.json",
            ):
                if not (project / "segmentation" / required).is_file():
                    raise RuntimeError(f"published segmentation file is missing: {required}")

            manifest = json.loads(
                (project / "run_manifest.json").read_text(encoding="utf-8")
            )
            public_payload = json.dumps(
                {
                    "task": task,
                    "segmentation_task": segmentation_task,
                    "segmentation_result": json.loads(
                        (project / "segmentation" / "segmentation_candidate.json").read_text(
                            encoding="utf-8"
                        )
                    ),
                    "evidence": evidence,
                },
                ensure_ascii=False,
            )
            host_audit_payload = json.dumps(
                {"canonical": json.loads(public_payload), "manifest": manifest},
                ensure_ascii=False,
            )
            if str(project) in host_audit_payload or str(media.parent) in host_audit_payload:
                raise RuntimeError("public task data leaked an absolute host path")
            lowered_public = public_payload.casefold()
            for retired_name in ("p2mix", "stage1", "experiment"):
                if retired_name in lowered_public:
                    raise RuntimeError(
                        f"canonical task data leaked retired name: {retired_name}"
                    )
            report = {
                "status": "passed",
                "source": {
                    "name": media.name,
                    "byte_size": media.stat().st_size,
                    "sha256": source_sha256,
                    "duration_seconds": evidence["media"]["duration_seconds"],
                },
                "project_id": job_id,
                "task_id": task_id,
                "task_state": task["state"],
                "segmentation_task_id": segmentation_task_id,
                "segmentation_task_state": segmentation_task["state"],
                "workflow_state": current["status"],
                "idempotent_replay": True,
                "recognition": {
                    "units": len(evidence["units"]),
                    "sentences": len(evidence["chunks"]),
                    "artifact_count": len(service.list_artifacts(task_id)),
                },
                "segmentation": {
                    "artifact_count": len(segmentation_artifacts),
                    "mode": segmentation_task["result"]["summary"]["mode"],
                    "review_required_count": segmentation_task["result"]["summary"][
                        "review_required_count"
                    ],
                },
                "editor": {
                    "revision_id": latest.revision_id,
                    "revision_number": latest.revision_number,
                    "cue_count": len(latest.document.cues),
                    "media_ready": True,
                    "waveform_ready": True,
                },
            }
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        finally:
            scheduler.shutdown(grace_seconds=1.0, timeout_seconds=10.0)
            if job_id:
                with application.JOBS_LOCK:
                    application.JOBS.pop(job_id, None)


if __name__ == "__main__":
    sys.exit(main())
