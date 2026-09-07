"""Compare batch replacement against the audited fc902d5 sequential reducer."""
from pathlib import Path
import json
import subprocess
import sys
import time
import types

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from substar_core.domain import SourceToken,DisplayToken,DisplayCue,EditorDocument,ChangeProvenance,ChangeKind
from substar_core.document_operations import apply_document_batch

legacy=types.ModuleType('audited_operations')
exec(subprocess.check_output(['git','show','fc902d5:substar_core/document_operations.py'],cwd=ROOT).decode('utf-8'),legacy.__dict__)
results=[]
for count in (500,2000,10000):
    provenance=ChangeProvenance(ChangeKind.SOURCE,'benchmark')
    sources=tuple(SourceToken.create(index=i,text='word',start=i*.1,end=i*.1+.08) for i in range(count*8))
    tokens=tuple(DisplayToken.create(position=i,text=t.text,source_token_ids=(t.token_id,),provenance=provenance) for i,t in enumerate(sources))
    cues=tuple(DisplayCue(cue_id=f'cue{i}',index=i,display_token_ids=tuple(t.token_id for t in tokens[i*8:i*8+8]),start=i*.8,end=(i+1)*.8) for i in range(count))
    document=EditorDocument('benchmark',sources,tokens,cues)
    operations=[{'operation_id':f'op{i}','type':'replace','payload':{'token_id':tokens[i].token_id,'expected_text':'word','text':'changed'}} for i in range(10)]
    start=time.perf_counter();candidate=document
    for operation in operations:candidate=legacy.apply_document_operation(candidate,operation)
    baseline=(time.perf_counter()-start)*1000
    start=time.perf_counter();candidate=apply_document_batch(document,operations)
    fixed=(time.perf_counter()-start)*1000
    candidate.validate()
    row={'cues':count,'tokens':count*8,'operations':10,'baseline_ms':round(baseline,2),'fixed_ms':round(fixed,2)}
    results.append(row);print(json.dumps(row),flush=True)
output=ROOT/'data'/'audit-validation'/'acceptance'/'backend-benchmark.json'
output.parent.mkdir(parents=True,exist_ok=True)
output.write_text(json.dumps(results,indent=2),encoding='utf-8')
