"""检索层的数据结构（CT-1 的响应形态）。

字段名与《契约》3.1 对齐，避免接口层再做一次映射。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Candidate:
    """单通道返回的候选块。"""

    chunk_id: str
    provision_id: str | None
    score: float
    channel: str
    page_no: int
    text: str
    doc_id: str = ""
    lang: str = ""

    def as_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "provision_id": self.provision_id,
            "score": round(self.score, 6),
            "channel": self.channel,
            "page_no": self.page_no,
            "doc_id": self.doc_id,
            "lang": self.lang,
            "text": self.text,
        }


@dataclass
class FusedHit:
    """RRF 融合后的候选。"""

    chunk_id: str
    provision_id: str | None
    rrf_score: float
    channels: list[str]
    page_no: int
    text: str
    doc_id: str = ""
    lang: str = ""
    channel_scores: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "provision_id": self.provision_id,
            "rrf_score": round(self.rrf_score, 6),
            "channels": list(self.channels),
            "channel_scores": {k: round(v, 6) for k, v in self.channel_scores.items()},
            "page_no": self.page_no,
            "doc_id": self.doc_id,
            "lang": self.lang,
            "text": self.text,
        }


@dataclass
class ProvisionHit:
    """条款级重排后的一席。同一条款只允许出现一次。"""

    provision_id: str
    score: float
    rank: int
    chunks: list[FusedHit] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "provision_id": self.provision_id,
            "score": round(self.score, 6),
            "rank": self.rank,
            "chunks": [c.as_dict() for c in self.chunks],
        }


@dataclass
class SearchResult:
    """一次检索的完整结果。"""

    candidates: list[Candidate] = field(default_factory=list)
    fused: list[FusedHit] = field(default_factory=list)
    reranked: list[ProvisionHit] = field(default_factory=list)
    sparse_disabled: str = ""
    empty_reason: str = ""
    channel_errors: dict[str, str] = field(default_factory=dict)
    rerank_skipped: bool = False
    index_version: str = ""
    query_lang: str = ""
    truncated: bool = False
