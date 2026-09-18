"""Bounded phonetic assistance for Chinese manuscript alignment.

Only compares identical untoned syllables inside already located text regions.
Source timing and display projection remain owned by manuscript_matching.
"""
from __future__ import annotations

import difflib
from functools import lru_cache
from pypinyin import lazy_pinyin, Style


def refine_opcodes(opcodes, source, reference, accepted_positions):
    """Preserve accepted replacements; realign only short unresolved Han spans."""
    anchors = [
        op for position, op in enumerate(opcodes)
        if (op[0] == "equal" and op[2] - op[1] >= 2)
        or (op[0] == "replace" and position in accepted_positions)
    ]
    result = []
    source_start = reference_start = 0
    end = ("equal", len(source), len(source), len(reference), len(reference))
    for tag, i1, i2, j1, j2 in [*anchors, end]:
        if i1 > source_start or j1 > reference_start:
            left = source[source_start:i1]
            right = reference[reference_start:j1]
            if max(len(left), len(right)) <= 12 and all(
                len(token) == 1 and '\u4e00' <= token <= '\u9fff'
                for token in left + right
            ):
                result.extend(weighted_align(left, right, source_start, reference_start, True))
            else:
                result.extend(
                    (kind, a + source_start, b + source_start, c + reference_start, d + reference_start)
                    for kind, a, b, c, d in difflib.SequenceMatcher(None, left, right, autojunk=False).get_opcodes()
                )
        if i2 > i1 or j2 > j1:
            result.append((tag, i1, i2, j1, j2))
        source_start, reference_start = i2, j2
    merged = []
    for op in result:
        if merged and merged[-1][0] == op[0] and merged[-1][2] == op[1] and merged[-1][4] == op[3]:
            previous = merged[-1]
            merged[-1] = (op[0], previous[1], op[2], previous[3], op[4])
        else:
            merged.append(op)
    return merged

@lru_cache(maxsize=8192)
def sound(s):
    if not s or not all(('一' <= c <= '鿿' for c in s)):
        return None
    return tuple(lazy_pinyin(s, style=Style.NORMAL))

def same_sound(a, b):
    return bool(sound(a) and sound(a) == sound(b))

def phonetic_score(ops, pos, a, b, result):
    _, i, k, j, l = ops[pos]
    if result['accepted'] or k - i != l - j or (not 0 < k - i <= 6):
        return result
    if not all((x == y or same_sound(x, y) for x, y in zip(a[i:k], b[j:l]))):
        return result
    left = pos - 1
    right = pos + 1
    while left >= 0 and ops[left][0] != 'equal':
        left -= 1
    while right < len(ops) and ops[right][0] != 'equal':
        right += 1
    anchored = result['left_anchor'] or result['right_anchor']
    if left >= 0 and right < len(ops):
        anchored = anchored or (ops[left][2] - ops[left][1] >= 1 and ops[right][2] - ops[right][1] >= 1 and (max(ops[right][1] - ops[left][2], ops[right][3] - ops[left][4]) <= 12))
    if anchored:
        result.update(accepted=True, score=max(result['score'], 0.74), phonetic_support=True)
    return result

def weighted_align(a, b, ai, bj, use_sound):

    def sub(x, y):
        return 0 if x == y else 1 if use_sound and same_sound(x, y) else 4
    n, m = (len(a), len(b))
    cost = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        cost[i][0] = 4 * i
    for j in range(m + 1):
        cost[0][j] = 4 * j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost[i][j] = min(cost[i - 1][j - 1] + sub(a[i - 1], b[j - 1]), cost[i - 1][j] + 4, cost[i][j - 1] + 4)
    out = []
    i = n
    j = m
    while i or j:
        if i and j and (cost[i][j] == cost[i - 1][j - 1] + sub(a[i - 1], b[j - 1])):
            out.append(('equal' if a[i - 1] == b[j - 1] else 'replace', ai + i - 1, ai + i, bj + j - 1, bj + j))
            i -= 1
            j -= 1
        elif i and cost[i][j] == cost[i - 1][j] + 4:
            out.append(('delete', ai + i - 1, ai + i, bj + j, bj + j))
            i -= 1
        else:
            out.append(('insert', ai + i, ai + i, bj + j - 1, bj + j))
            j -= 1
    return list(reversed(out))
