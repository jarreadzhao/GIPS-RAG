"""AC-5：双语对照文件切分 —— 每个 chunk 同时含中英，pair_id 唯一且 lang 正确。"""
from __future__ import annotations

import numpy as np

from app.parse.bilingual import Para, build_bilingual_chunks, build_pairs


def _encoder(texts: list[str]) -> np.ndarray:
    """按关键词构造 one-hot，使同义的中英段落相似度为 1。"""
    keys = (("policy", "政策"), ("record", "记录"), ("perform", "业绩"))
    out = []
    for text in texts:
        vec = np.zeros(8, dtype=np.float32)
        for i, group in enumerate(keys):
            if any(k in text.lower() for k in group):
                vec[i] = 1.0
                break
        else:
            vec[7] = 1.0
        out.append(vec)
    return np.vstack(out)


def test_pairs_align_one_to_one():
    """章节内 1:1 对齐应得到三条中英齐备的 pair。"""
    en = [Para("The firm must document policies.", 1),
          Para("The firm must retain records.", 2),
          Para("The firm must present performance.", 3)]
    zh = [Para("机构应当书面记录政策。", 1),
          Para("机构应当保存记录。", 2),
          Para("机构应当展示业绩。", 3)]
    pairs = build_pairs(en, zh, _encoder)
    assert len(pairs) == 3
    assert all(p["zh"] for p in pairs)


def test_bilingual_chunks_contain_both_languages():
    """装箱后每个 chunk 同时含中文与英文，lang=mixed，pair_id 唯一。"""
    en = [Para("The firm must document policies.", 1)]
    zh = [Para("机构应当书面记录政策。", 1)]
    pairs = build_pairs(en, zh, _encoder)
    chunks = build_bilingual_chunks(pairs)
    assert chunks
    for c in chunks:
        assert "机构应当书面记录政策" in c["text"]
        assert "document policies" in c["text"]
        assert c["lang"] == "mixed"
    ids = [c["pair_id"] for c in chunks]
    assert len(ids) == len(set(ids))


def test_unpaired_side_is_not_silently_dropped():
    """只有英文侧的章节也要产出 pair，并显式标 en_only。"""
    en = [Para("Only English exists here.", 1)]
    pairs = build_pairs(en, [], _encoder)
    assert pairs[0]["merge"] == "en_only"
    assert pairs[0]["zh"] == ""
