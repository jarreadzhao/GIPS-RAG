"""条款级双语配对（R-13 / AC-5）。

规则必须可解释、可复现：

1. 两侧都带 `provision_id` 的 chunk，按条款分组；
2. 同一条款两侧块数相等 → 按 (page_start, char_start) 顺序 1:1 配对；
   块数不等 → 整条条款共用一个 pair_id（多对多），宁可多显示一段，
   也不把「这一句的中文」配到隔壁句；
3. 没有 `provision_id` 的块（封面、目录）不配对，保持 NULL。

不做句级/长度比例硬对齐：中英断句位置本来就不同，按长度强行切分会在
条款内部错位，引用对照一旦指错，比多给一段更糟。
"""
from __future__ import annotations

from typing import Any


def assign_pair_ids(en_chunks: list[dict[str, Any]],
                    zh_chunks: list[dict[str, Any]]) -> dict[str, str]:
    """返回 chunk_id → pair_id；不在两侧共有条款内的块不出现在结果里。"""
    en_by = _group(en_chunks)
    zh_by = _group(zh_chunks)
    mapping: dict[str, str] = {}
    for provision_id in sorted(set(en_by) & set(zh_by)):
        en_side, zh_side = en_by[provision_id], zh_by[provision_id]
        if len(en_side) == len(zh_side):
            for index, (en, zh) in enumerate(zip(en_side, zh_side), start=1):
                pair_id = f"pr:{provision_id}:{index}"
                mapping[en["chunk_id"]] = pair_id
                mapping[zh["chunk_id"]] = pair_id
        else:
            pair_id = f"pr:{provision_id}"
            for chunk in en_side + zh_side:
                mapping[chunk["chunk_id"]] = pair_id
    return mapping


def pairing_status(db: Any) -> dict[str, int]:
    """统计两侧的配对覆盖度，供启动自检判断配对键是否丢失。

    只读 SQLite，不碰向量库；空库各项为 0，调用方不必特判。
    """
    row = db.fetch_one(
        "SELECT "
        "SUM(CASE WHEN lang='en' AND status='active' THEN 1 ELSE 0 END) AS en_total, "
        "SUM(CASE WHEN lang='en' AND status='active' "
        "AND pair_id IS NOT NULL AND pair_id<>'' THEN 1 ELSE 0 END) AS en_paired, "
        "SUM(CASE WHEN lang='zh' AND status='active' THEN 1 ELSE 0 END) AS zh_total, "
        "SUM(CASE WHEN lang='zh' AND status='active' "
        "AND pair_id IS NOT NULL AND pair_id<>'' THEN 1 ELSE 0 END) AS zh_paired "
        "FROM chunks")
    return {k: int(row[k] or 0) for k in
            ("en_total", "en_paired", "zh_total", "zh_paired")}


def _group(chunks: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks:
        provision_id = str(chunk.get("provision_id") or "")
        if provision_id:
            grouped.setdefault(provision_id, []).append(chunk)
    for items in grouped.values():
        items.sort(key=lambda c: (int(c.get("page_start") or 0),
                                  int(c.get("char_start") or 0),
                                  str(c.get("chunk_id"))))
    return grouped
