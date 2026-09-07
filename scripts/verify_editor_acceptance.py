"""Download and validate revision-pinned artifacts from a running editor."""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from substar_core.domain import EditorDocument


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8879')
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--many-revision', required=True)
    parser.add_argument('--line-revision', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    base = args.base_url.rstrip('/') + '/api/projects/' + args.project_id
    summary = {}
    source_tokens = None
    for label, revision_id in [('many-to-many', args.many_revision), ('one-to-one', args.line_revision)]:
        response = requests.get(base + '/revisions/' + revision_id, timeout=30)
        response.raise_for_status()
        revision = response.json()
        doc = EditorDocument.from_dict(revision['document'])
        doc.validate()
        cues = [cue for cue in doc.cues if cue.state.value == 'active']
        assert cues and all(cue.target and cue.target.target_text.strip() for cue in cues)
        assert all(token.timing and token.timing.get('evidence_sha256') for token in doc.source_tokens)
        if source_tokens is not None:
            assert revision['document']['source_tokens'] == source_tokens, 'translation changed source timing'
        source_tokens = revision['document']['source_tokens']
        (args.output_dir / f'{label}-revision.json').write_text(json.dumps(revision, ensure_ascii=False, indent=2), encoding='utf-8')
        exports = {}
        for file_mode, api_mode in [('a', 'source'), ('b', 'target'), ('ab_double', 'ab-double')]:
            response = requests.get(base + '/export/' + api_mode, params={'revision_id':revision_id}, timeout=30)
            response.raise_for_status()
            assert 'application/x-subrip' in response.headers['Content-Type']
            content = response.content.decode('utf-8-sig')
            blocks = content.strip().split('\n\n')
            assert len(blocks) == len(cues)
            assert all('-->' in block and len(block.splitlines()) >= 3 for block in blocks)
            (args.output_dir / f'{label}-{file_mode}.srt').write_bytes(response.content)
            exports[file_mode] = {'cues':len(blocks), 'bytes':len(response.content)}
        summary[label] = {'revision_id':revision_id, 'source_tokens':len(doc.source_tokens),
                          'cues':len(cues), 'review_cues':[cue.cue_id for cue in cues if cue.target.translation_status!='translated'],
                          'exports':exports}
    response = requests.get(base + '/exchange/subtitle-project', params={'revision_id':args.line_revision}, timeout=60)
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.testzip() is None
        revisions = [name for name in archive.namelist() if name.endswith('latest.json')]
        assert revisions, archive.namelist()
        value = json.loads(archive.read(revisions[0]))
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['revision_id'] == args.line_revision
        EditorDocument.from_dict(value).validate()
        assert value == revision['document']
        summary['project_snapshot'] = {'bytes':len(response.content), 'files':archive.namelist(), 'revision_id':manifest['revision_id']}
    (args.output_dir/'video-subtitle-project.zip').write_bytes(response.content)
    (args.output_dir/'artifact-verification.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
