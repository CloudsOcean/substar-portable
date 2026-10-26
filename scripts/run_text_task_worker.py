from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from substar_core.editor.text_task_runtime import main

if __name__ == "__main__":
    raise SystemExit(main())
