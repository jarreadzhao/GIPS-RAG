"""双语配对规则测试（R-13 / AC-5）。"""
from __future__ import annotations

from app.index.pairing import assign_pair_ids


def _chunk(chunk_id: str, provision_id: str | None, page: int = 1,
           char_start: int = 0) -> dict:
    return {"chunk_id": chunk_id, "provision_id": provision_id,
            "page_start": page, "char_start": char_start}


def test_equal_counts_pair_one_to_one() -> None:
    en = [_chunk("e1", "1.A.5", 19, 0), _chunk("e2", "1.A.5", 19, 300)]
    zh = [_chunk("z1", "1.A.5", 7, 0), _chunk("z2", "1.A.5", 7, 120)]
    mapping = assign_pair_ids(en, zh)
    assert mapping == {
        "e1": "pr:1.A.5:1", "z1": "pr:1.A.5:1",
        "e2": "pr:1.A.5:2", "z2": "pr:1.A.5:2",
    }


def test_unequal_counts_share_one_pair_id() -> None:
    en = [_chunk("e1", "1.A.5", 19, 0), _chunk("e2", "1.A.5", 19, 300)]
    zh = [_chunk("z1", "1.A.5", 7, 0)]
    mapping = assign_pair_ids(en, zh)
    assert mapping == {"e1": "pr:1.A.5", "e2": "pr:1.A.5", "z1": "pr:1.A.5"}


def test_chunks_without_provision_are_not_paired() -> None:
    en = [_chunk("e1", "1.A.5"), _chunk("cover", None)]
    zh = [_chunk("z1", "1.A.5"), _chunk("cover-zh", None)]
    mapping = assign_pair_ids(en, zh)
    assert "cover" not in mapping and "cover-zh" not in mapping
    assert mapping["e1"] == "pr:1.A.5:1"


def test_provision_present_on_one_side_only_is_not_paired() -> None:
    mapping = assign_pair_ids([_chunk("e1", "7.C.32")], [])
    assert mapping == {}
