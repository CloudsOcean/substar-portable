import cProfile, io, json, pstats, sqlite3, time, uuid
from pathlib import Path
import requests
from substar_core.storage.project_store import ProjectStore
from substar_core.editor.application.editing_service import EditingService
from substar_core.editor.infrastructure.sqlite_project_repository import SQLiteProjectRepository

out = Path(__file__).parent
baseline = json.loads((out / 'baseline.json').read_text('utf-8'))
project = baseline['copy']
store = ProjectStore(Path('data/projects-v2') / project / 'project')
service = EditingService(lambda _: SQLiteProjectRepository(store))
results = {'project': project, 'profile_operations': [], 'topology_operations': []}

def base(r):
    return {'document_id': r.document.document_id, 'revision_id': r.revision_id, 'document_hash': r.document_hash}

profile = cProfile.Profile()
for number in range(3):
    r = store.load_latest()
    token = next(t for t in r.document.display_tokens[400:] if t.state.value == 'active')
    operation = {'operation_id': 'op_profile_' + uuid.uuid4().hex, 'type': 'replace', 'base': base(r),
                 'payload': {'token_id': token.token_id, 'expected_text': token.text, 'text': token.text + '剖析',
                             'provenance': {'kind': 'manual', 'operation': 'stress_profile'}}}
    started = time.perf_counter()
    profile.enable()
    service.commit_batch(project, base=base(r), operations=[operation], batch_id=uuid.uuid4().hex)
    profile.disable()
    results['profile_operations'].append((time.perf_counter() - started) * 1000)
stream = io.StringIO()
pstats.Stats(profile, stream=stream).strip_dirs().sort_stats('cumulative').print_stats(40)
(out / 'backend-profile.txt').write_text(stream.getvalue(), 'utf-8')

def commit(kind, payload):
    r = store.load_latest()
    operation = {'operation_id': 'op_topology_' + uuid.uuid4().hex, 'type': kind, 'base': base(r),
                 'payload': {**payload, 'provenance': {'kind': 'manual', 'operation': 'stress_' + kind}}}
    started = time.perf_counter()
    response = requests.post(f'http://127.0.0.1:8769/api/projects/{project}/operations', json=operation, timeout=30)
    results['topology_operations'].append({'type': kind, 'status': response.status_code,
                                          'ms': (time.perf_counter() - started) * 1000})
    response.raise_for_status()
    return store.load_latest()

r = store.load_latest()
cue = r.document.cues[10]
original_ids = cue.display_token_ids
right_id = 'cue_stress_' + uuid.uuid4().hex
r = commit('split_cue', {'cue_id': cue.cue_id, 'after_token_id': original_ids[1], 'right_cue_id': right_id})
assert next(c for c in r.document.cues if c.cue_id == right_id)
r = commit('merge_cues', {'cue_ids': [cue.cue_id, right_id]})
assert next(c for c in r.document.cues if c.cue_id == cue.cue_id).display_token_ids == original_ids
token_id = original_ids[0]
r = commit('delete', {'token_ids': [token_id]})
assert next(t for t in r.document.display_tokens if t.token_id == token_id).state.value == 'deleted'
r = commit('restore', {'token_ids': [token_id]})
assert next(t for t in r.document.display_tokens if t.token_id == token_id).state.value == 'active'
with sqlite3.connect(store.database_path) as db:
    rows = db.execute('SELECT revision_number, length(snapshot_blob), length(patch_blob) FROM revisions').fetchall()
results['storage'] = {'revisions': len(rows), 'database_bytes': store.database_path.stat().st_size,
                      'snapshot_sizes': [a for _, a, _ in rows if a], 'patch_sizes': [b for _, _, b in rows if b]}
original = ProjectStore(Path('data/projects-v2') / baseline['original'] / 'project').load_latest()
results['original_unchanged'] = original.revision_id == baseline['original_revision'] and original.document_hash == baseline['original_hash']
(out / 'backend-results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), 'utf-8')
print(json.dumps({key: value for key, value in results.items() if key != 'storage'}, ensure_ascii=False))
