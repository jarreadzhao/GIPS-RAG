"""RRF 融合（PRD 5.5）。

三路分数不可比：稠密是余弦、稀疏是 IP、结构化是固定分。
因此只按名次融合，`1/(k+rank)` 求和，k 固定 60，不做分数加权。
"""
from __future__ import annotations

from app.retrieve.types import Candidate, FusedHit


def rrf_fuse(channel_results: list[tuple[str, list[Candidate]]], *, k: int = 60
             ) -> list[FusedHit]:
    """把各通道结果融合为一条有序列表。

    channel_results 的顺序决定同分时的稳定次序（dense → sparse → sql）。
    同一 chunk 被多个通道召回时，分数相加，来源通道全部记录。
    """
    merged: dict[str, FusedHit] = {}
    order: dict[str, int] = {}
    cursor = 0
    for channel, candidates in channel_results:
        for rank, cand in enumerate(candidates, start=1):
            score = 1.0 / (k + rank)
            existing = merged.get(cand.chunk_id)
            if existing is None:
                merged[cand.chunk_id] = FusedHit(
                    chunk_id=cand.chunk_id,
                    provision_id=cand.provision_id,
                    rrf_score=score,
                    channels=[channel],
                    page_no=cand.page_no,
                    text=cand.text,
                    doc_id=cand.doc_id,
                    lang=cand.lang,
                    channel_scores={channel: cand.score},
                )
                order[cand.chunk_id] = cursor
                cursor += 1
            else:
                existing.rrf_score += score
                if channel not in existing.channels:
                    existing.channels.append(channel)
                existing.channel_scores[channel] = cand.score
                # 结构化通道可能先给出条款级摘要，保留信息量更大的正文
                if len(cand.text) > len(existing.text):
                    existing.text = cand.text
                    existing.page_no = cand.page_no

    return sorted(merged.values(), key=lambda h: (-h.rrf_score, order[h.chunk_id]))


def group_by_provision(fused: list[FusedHit]) -> list[tuple[str, list[FusedHit]]]:
    """按条款聚合，组内保持 RRF 顺序，组间按首次出现顺序。

    没有 provision_id 的块以 `chunk:<id>` 作为伪条款，避免它们被并成一组。
    """
    groups: dict[str, list[FusedHit]] = {}
    order: list[str] = []
    for hit in fused:
        key = hit.provision_id or f"chunk:{hit.chunk_id}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(hit)
    return [(key, groups[key]) for key in order]
