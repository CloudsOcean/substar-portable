"""Exercise the real replacement helper against disposable miniature installations."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows updater")
@pytest.mark.parametrize("fail",[False,True])
def test_helper_replaces_or_rolls_back_without_touching_data(tmp_path,fail):
    root = tmp_path / "字幕 软件"
    stage = tmp_path / "更新 暂存"
    base = Path(sys.base_prefix)
    for directory,version in ((root,"old"),(stage,"new")):
        runtime = directory / "runtime/python"
        runtime.mkdir(parents=True)
        for name in ("python.exe","python312.dll","python311.dll","python3.dll","vcruntime140.dll","vcruntime140_1.dll"):
            if (base / name).exists(): shutil.copyfile(base / name,runtime / name)
        (runtime / "pyvenv.cfg").write_text(f"home = {base}\ninclude-system-site-packages = false\n",encoding="utf-8")
        (directory / "app.py").write_text(version)
        (directory / "launcher.py").write_text("import sys\nraise SystemExit("+
            ("7" if fail and version == "new" else "0")+")\n")
    (root / "data").mkdir()
    (root / "data/user-project.json").write_text('preserve me')
    (root / "models").mkdir()
    (root / "models/custom.bin").write_bytes(b'original model')
    plan_path = tmp_path / "pending.json"
    outcome = tmp_path / "outcome.json"
    plan = {"install":str(root),"staged":str(stage),"backup":str(tmp_path / "backup"),
            "roots":["app.py","launcher.py","runtime"],"outcome":str(outcome),"version":"2.3.0"}
    plan_path.write_text(json.dumps(plan,ensure_ascii=False),encoding="utf-8")
    original = Path(__file__).resolve().parents[1] / "substar_core/update_helper.ps1"
    helper = tmp_path / "helper.ps1"
    helper.write_text(original.read_text(encoding="utf-8"),encoding="utf-8-sig")
    result = subprocess.run(["powershell.exe","-NoProfile","-ExecutionPolicy","Bypass","-File",str(helper),
                             "-PlanPath",str(plan_path),"-LauncherPid","2147483647"],
        capture_output=True,text=True,timeout=90,creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr
    status = json.loads(outcome.read_text(encoding="utf-8-sig"))
    assert status["status"] == ("rolled_back" if fail else "succeeded"), status
    assert (root / "app.py").read_text() == ("old" if fail else "new")
    assert (root / "data/user-project.json").read_text() == 'preserve me'
    assert (root / "models/custom.bin").read_bytes() == b'original model'
    assert not plan_path.exists()
