import app


def test_list_and_persistence_never_prepare_exports(tmp_path, monkeypatch):
    job = app.Job(id="sample", filename="sample.mp4", job_dir=tmp_path,
                  input_path=tmp_path / "sample.mp4", workflow_mode="subtitle_creation",
                  status="awaiting_edit")
    def forbidden(*args, **kwargs):
        raise AssertionError("list/persistence must not prepare subtitle exports")
    monkeypatch.setattr(app, "_workbench_export_availability", forbidden)
    monkeypatch.setattr(app, "_restore_persisted_jobs", lambda: None)
    monkeypatch.setattr(app, "_prune_missing_jobs", lambda: None)
    monkeypatch.setattr(app, "_refresh_canonical_job_projection", lambda job: None)
    monkeypatch.setattr(app, "JOBS", {job.id: job})
    app._persist_job(job)
    assert app.list_jobs()[0]["export_availability"] is None


def test_individual_job_still_checks_exports(tmp_path, monkeypatch):
    job = app.Job(id="sample", filename="sample.mp4", job_dir=tmp_path,
                  input_path=tmp_path / "sample.mp4")
    monkeypatch.setattr(app, "_workbench_export_availability", lambda job: {"checked": True})
    assert job.public()["export_availability"] == {"checked": True}
