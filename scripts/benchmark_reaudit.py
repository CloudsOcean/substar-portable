"""Measure real inline saves, including per-action revisions and SQLite commit."""
from pathlib import Path
import argparse
import json
import subprocess
import sys
import tempfile
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
parser=argparse.ArgumentParser()
parser.add_argument("--baseline", action="store_true")
args=parser.parse_args()
if args.baseline:
    for name in ("substar_core.editor.application.text_retiming", "substar_core.document_operations", "substar_core.editor.application.editing_service"):
        path=name.replace(".","/")+".py"
        module=types.ModuleType(name);module.__file__=str(ROOT/path)
        sys.modules[name]=module
        exec(subprocess.check_output(["git","show","40ece03:"+path],cwd=ROOT).decode("utf-8"),module.__dict__)
from substar_core.domain import SourceToken,DisplayToken,DisplayCue,EditorDocument,ChangeProvenance
from substar_core.editor.application.editing_service import EditingService
from substar_core.editor.infrastructure.sqlite_project_repository import SQLiteProjectRepository
from substar_core.storage import ProjectStore

for count in (500,2000,10000):
    provenance=ChangeProvenance("source","benchmark")
    sources=tuple(SourceToken.create(index=i,text="word",start=i*.1,end=i*.1+.08) for i in range(count*8))
    tokens=tuple(DisplayToken.create(position=i,text="word",source_token_ids=(t.token_id,),provenance=provenance) for i,t in enumerate(sources))
    cues=tuple(DisplayCue(f"cue{i}",i,tuple(t.token_id for t in tokens[i*8:i*8+8]),i*.8,(i+1)*.8) for i in range(count))
    doc=EditorDocument("benchmark",sources,tokens,cues)
    with tempfile.TemporaryDirectory(prefix="substar-reaudit-") as temporary:
        store=ProjectStore.create(Path(temporary)/"project",project_id="benchmark")
        rev=store.save(doc,provenance=ChangeProvenance("import","fixture"))
        operations=[{"operation_id":f"op{i}","type":"replace","payload":{"token_id":tokens[i].token_id,"expected_text":"word","text":"Word","provenance":{"kind":"manual","operation":"inline_replace"}}} for i in range(10)]
        base={"document_id":doc.document_id,"revision_id":rev.revision_id,"document_hash":rev.document_hash}
        service=EditingService(lambda _:SQLiteProjectRepository(store))
        start=time.perf_counter()
        saved=service.commit_batch("benchmark",base=base,operations=operations,batch_id="batch",preserve_user_actions=True)
        elapsed=time.perf_counter()-start
        assert len(store.list_revision_metadata())==11
        assert len(saved.action_revisions)==10
        print(json.dumps({"baseline":args.baseline,"cues":count,"tokens":count*8,"operations":10,"seconds":round(elapsed,3),"revisions":11}),flush=True)
