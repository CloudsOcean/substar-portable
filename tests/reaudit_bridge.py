"""JSON bridge for real JS factory/queue -> EditingService -> SQLite tests."""
from pathlib import Path
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_architecture_audit import document
from substar_core.domain import ChangeProvenance
from substar_core.storage import ProjectStore
from substar_core.editor.application.editing_service import EditingService
from substar_core.editor.infrastructure.sqlite_project_repository import SQLiteProjectRepository
value=json.load(sys.stdin)
root=Path(value["root"])/"project"
if value["mode"]=="init":
    store=ProjectStore.create(root,project_id="bridge")
    revision=store.save(document(),provenance=ChangeProvenance("import","fixture"))
else:
    store=ProjectStore.open(root)
    if value["mode"]=="commit":
        batch=value["batch"]
        revision=EditingService(lambda _:SQLiteProjectRepository(store)).commit_batch("bridge",base=batch["base"],operations=batch["operations"],batch_id=batch["batch_id"],preserve_user_actions=True).after
    else: revision=store.load_latest()
result=revision.to_dict();result["document_hash"]=revision.document_hash
print(json.dumps({"revision":result,"count":len(store.list_revision_metadata())}))
