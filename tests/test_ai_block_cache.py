from __future__ import annotations

from substar_core.ai_block_cache import (
    fingerprint,
    load_ai_block_cache,
    save_ai_block_cache,
)


def test_ai_block_cache_is_keyed_by_the_complete_frozen_request(tmp_path) -> None:
    first = fingerprint({"revision": "r1", "block": "b1", "model": "m1"})
    changed = fingerprint({"revision": "r2", "block": "b1", "model": "m1"})
    assert first != changed

    save_ai_block_cache(tmp_path, first, {"actions": [{"action_id": "a1"}]})

    assert load_ai_block_cache(tmp_path, first) == {
        "actions": [{"action_id": "a1"}]
    }
    assert load_ai_block_cache(tmp_path, changed) is None
