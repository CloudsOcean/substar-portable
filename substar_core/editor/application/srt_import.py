"""Import exported SRT translation tracks without changing source topology."""
from bisect import bisect_left, bisect_right
from dataclasses import replace
import hashlib
import json
import re

from substar_core.domain import ChangeKind, ChangeProvenance, EntityState, TranslationTrack
from substar_core.document_operations import synchronize_translation_status
from substar_core.export import render_document_srt
from substar_core.prompt_registry import source_language_analysis
from substar_core.storage import ProjectConflictError

STAMP = r'(\d{2,}):(\d{2}):(\d{2})[,\.](\d{3})'
TIMING = re.compile(r'^' + STAMP + r'\s+-->\s+' + STAMP + r'\s*$')
MODES = {'auto', 'source', 'target', 'ab-single', 'ab-double'}


def parse_srt(text):
    text = text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n').strip()
    if not text or '\ufffd' in text:
        raise ValueError('请选择有效的 UTF-8 或 UTF-16 SRT 文件')
    rows = []
    for block in re.split(r'\n[ \t]*\n+', text):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit():
            raise ValueError('SRT 格式无效：每条须包含序号、时间轴和文字')
        match = TIMING.fullmatch(lines[1].strip())
        if not match:
            raise ValueError(f'SRT 第 {lines[0]} 条时间轴无效')
        values = list(map(int, match.groups()))
        def milliseconds(v):
            h, m, s, ms = v
            if m >= 60 or s >= 60:
                raise ValueError('SRT 分钟或秒数无效')
            return ((h * 60 + m) * 60 + s) * 1000 + ms
        start, end = milliseconds(values[:4]), milliseconds(values[4:])
        body = '\n'.join(lines[2:]).strip()
        if end <= start or not body:
            raise ValueError('SRT 包含空字幕或无效时间范围')
        rows.append({'number': lines[0].strip(), 'start': start, 'end': end, 'text': body})
    if len(rows) > 20000:
        raise ValueError('SRT 最多支持 20000 条')
    return rows


def primary_language(text):
    analysis = source_language_analysis(text)
    return analysis['primary_language'] if analysis['language_character_count'] else 'unknown'


def norm(text):
    return re.sub(r'\s+', ' ', text).strip()


def extract_target(text, source, mode):
    if mode == 'target':
        return text
    if mode == 'ab-single':
        value, prefix = text.strip(), source.strip()
        if value.startswith(prefix + ' '):
            return value[len(prefix):].strip()
        return None
    if mode == 'ab-double':
        lines = text.splitlines()
        if len(lines) == 2:
            hits = [i for i, line in enumerate(lines) if norm(line) == norm(source)]
            if hits:
                return lines[1 - hits[0]].strip()
        return None
    return None


def preview_srt(document, text, mode='auto'):
    if mode not in MODES:
        raise ValueError('不支持的字幕格式')
    rows = parse_srt(text)
    cues = sorted((c for c in document.cues if c.state is EntityState.ACTIVE), key=lambda c: c.start)
    # Use the export projection, including punctuation and Chinese-script preferences.
    source_rows = parse_srt(render_document_srt(document, 'source'))
    if mode in {'auto', 'ab-single'} and len(rows) > len(source_rows) and all(
        (row['start'], row['end'], norm(row['text'])) == (source['start'], source['end'], norm(source['text']))
        for row, source in zip(rows, source_rows)
    ):
        # Standard track-major AB export: source prefix is verified, not guessed
        # from a time reset or a 50/50 record count.
        target_blocks = text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n').strip()
        target_text = '\n\n'.join(re.split(r'\n[ \t]*\n+', target_blocks)[len(source_rows):])
        result = preview_srt(document, target_text, mode='target')
        result['format'] = 'ab-single'
        return result
    by_time = {(r['start'], r['end']): r['text'] for r in source_rows}
    starts = [round(c.start * 1000) for c in cues]
    matches = []
    for row in rows:
        candidates = [c for c in cues[bisect_left(starts, row['start'] - 120):bisect_right(starts, row['start'] + 120)]
                      if abs(round(c.end * 1000) - row['end']) <= 120]
        cue = candidates[0] if len(candidates) == 1 else None
        source = by_time.get((round(cue.start * 1000), round(cue.end * 1000)), '') if cue else ''
        matches.append((row, cue, source))
    source_language = primary_language('\n'.join(r['text'] for r in source_rows))
    if mode == 'auto':
        eligible = [(r, c, s) for r, c, s in matches if c and s]
        if not eligible:
            mode = 'target'
        elif all(norm(r['text']) == norm(s) for r,c,s in eligible):
            mode = 'source'
        else:
            scores = {m: sum(extract_target(r['text'], s, m) is not None for r,c,s in eligible)
                      for m in ('ab-single', 'ab-double')}
            best = max(scores, key=scores.get)
            mode = best if scores[best] / len(eligible) >= .6 else 'target'
    uses = {}
    for _, cue, _ in matches:
        if cue:
            uses[cue.cue_id] = uses.get(cue.cue_id, 0) + 1
    extracted = [extract_target(row['text'], source, mode) if mode != 'source' else None
                 for row, cue, source in matches]
    # Track-wide decision only: never strip individual same-language sentences.
    target_language = primary_language('\n'.join(t for t in extracted if t))
    same_language = target_language == source_language
    preview = []
    for (row, cue, source), target in zip(matches, extracted):
        reason = ''
        if mode == 'source': reason = '原文轨道，不导入译文'
        elif not cue or not source: reason = '时间轴未唯一匹配（支持前后 120 毫秒偏差）'
        elif uses[cue.cue_id] > 1: reason = '多条 SRT 对应同一字幕，请调整后重新导入'
        elif not target: reason = '无法按当前原文分离双语轨道'
        elif same_language: reason = '轨道主要语言与节目原文相同，不导入'
        elif target_language == 'unknown': reason = '无法判断轨道语言'
        preview.append({**row, 'cue_id': cue.cue_id if cue else None, 'source': source,
                        'target': target or '', 'reason': reason, 'importable': not reason,
                        'overwrite': bool(not reason and cue.target and cue.target.target_text)})
    return {'format': mode, 'source_language': source_language, 'target_language': target_language,
            'rows': preview, 'matched': sum(r['importable'] for r in preview),
            'skipped': sum(not r['importable'] for r in preview),
            'overwrites': sum(r['overwrite'] for r in preview)}


def import_srt(store, *, text, mode, expected_revision_id, request_id):
    digest = hashlib.sha256(json.dumps([text, mode, expected_revision_id], ensure_ascii=False).encode()).hexdigest()
    receipt_id = 'srt-import:' + request_id
    prior = store.find_receipt(receipt_id, digest)
    if prior is not None:
        return prior
    latest = store.load_latest()
    if latest is None or latest.revision_id != expected_revision_id:
        raise ProjectConflictError('项目已变化，请重新预览后导入')
    preview = preview_srt(latest.document, text, mode)
    targets = {row['cue_id']: row['target'] for row in preview['rows'] if row['importable']}
    if not targets:
        raise ValueError('没有可导入的译文')
    provenance = ChangeProvenance(kind=ChangeKind.IMPORT, operation='srt_translation_import', actor='user',
                                 metadata={'format': preview['format'], 'count': len(targets), 'request_id': request_id})
    cues = tuple(replace(c, target=TranslationTrack(target_text=targets[c.cue_id], original_text=targets[c.cue_id],
                        language=preview['target_language'], provenance=provenance)) if c.cue_id in targets else c
                 for c in latest.document.cues)
    candidate = replace(latest.document, cues=cues, changes=(*latest.document.changes, provenance))
    candidate = synchronize_translation_status(candidate, set(targets))
    return store.save(candidate, provenance=provenance, expected_revision_id=expected_revision_id,
                      receipt={'id': receipt_id, 'hash': digest, 'result': {'count': len(targets)}})
