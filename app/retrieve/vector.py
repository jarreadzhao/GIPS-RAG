"""向量后端（稠密 / 稀疏）与 Milvus 过滤表达式构造。

过滤必须前置：`acl_scope`、`corpus_type`、`index_version`、`effective_at`
全部拼进 search 的 filter，不允许「先召回再筛」。后置过滤会把 Top-K 筛空，
用户看到的是「没找到」而不是「无权访问」（PRD 第 3 章第 3 条）。
"""
from __future__ import annotations

import re
from typing import Protocol

from app.config import Settings
from app.errors import AppError
from app.retrieve.types import Candidate

# 标签只允许这些字符；含引号/空格的标签直接丢弃（fail-closed）
_LABEL_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

OUTPUT_FIELDS = [
    "doc_id", "provision_id", "chunk_type", "lang",
    "page_start", "char_start", "char_end", "text",
]


class VectorSearcher(Protocol):
    """向量检索协议；单测用内存实现替换 Milvus。"""

    def dense(self, vector: list[float], expr: str, limit: int) -> list[Candidate]:
        """稠密向量检索。"""

    def sparse(self, weights: dict[int, float], expr: str, limit: int) -> list[Candidate]:
        """稀疏向量检索。"""

    def probe(self, expr: str) -> int:
        """返回满足 expr 的条数，用于区分 acl_filtered 与 no_match。"""


def _quote(values: list[str]) -> str:
    """把字符串列表拼成 Milvus 的 ["a","b"] 形式。"""
    return "[" + ", ".join(f'"{v}"' for v in values) + "]"


def build_filter(index_versions: list[str], *, corpus_type: str | None = None,
                 lang: str | None = None, doc_ids: list[str] | None = None) -> str:
    """构造与 ACL 无关的基础过滤条件。"""
    parts = [f"index_version in {_quote(index_versions)}"]
    if corpus_type:
        parts.append(f'corpus_type == "{corpus_type}"')
    if lang:
        parts.append(f'lang == "{lang}"')
    if doc_ids is not None:
        parts.append(f"doc_id in {_quote(doc_ids)}")
    return " and ".join(parts)


def build_acl_expr(base: str, depts: list[str], clearances: list[str]) -> str | None:
    """在基础条件上追加 ACL；返回 None 表示该 scope 不可能命中任何行。

    acl_scope 是「部门 + 密级」的合取标签：用户必须同时具备部门与密级标签。
    """
    clean_depts = [d for d in depts if _LABEL_RE.match(d)]
    clean_clrs = [c for c in clearances if _LABEL_RE.match(c)]
    if not clean_depts or not clean_clrs:
        return None
    return (f"{base} and dept in {_quote(clean_depts)} "
            f"and clearance in {_quote(sorted(set(clean_clrs)))}")


def _row_to_candidate(hit: dict, *, channel: str, score: float) -> Candidate:
    """把 Milvus 返回项转成 Candidate。"""
    entity = hit.get("entity", hit)
    provision = entity.get("provision_id") or None
    return Candidate(
        chunk_id=str(hit.get("id") or entity.get("chunk_id") or ""),
        provision_id=provision,
        score=score,
        channel=channel,
        page_no=int(entity.get("page_start") or 0),
        text=str(entity.get("text") or ""),
        doc_id=str(entity.get("doc_id") or ""),
        lang=str(entity.get("lang") or ""),
    )


class MilvusVectorSearcher:
    """基于 pymilvus MilvusClient 的检索实现。"""

    def __init__(self, settings: Settings) -> None:
        from pymilvus import MilvusClient

        self._client = MilvusClient(uri=settings.milvus_uri)
        self._collection = settings.milvus_collection

    def dense(self, vector: list[float], expr: str, limit: int) -> list[Candidate]:
        """稠密检索（HNSW / COSINE），filter 为前置条件。"""
        res = self._client.search(
            collection_name=self._collection,
            data=[vector],
            anns_field="dense",
            search_params={"metric_type": "COSINE", "params": {"ef": 64}},
            limit=limit,
            filter=expr,
            output_fields=OUTPUT_FIELDS,
        )
        return [_row_to_candidate(h, channel="dense", score=float(h.get("distance", 0.0)))
                for h in (res[0] if res else [])]

    def sparse(self, weights: dict[int, float], expr: str, limit: int) -> list[Candidate]:
        """稀疏检索（SPARSE_INVERTED_INDEX / IP）。"""
        res = self._client.search(
            collection_name=self._collection,
            data=[weights],
            anns_field="sparse",
            search_params={"metric_type": "IP", "params": {}},
            limit=limit,
            filter=expr,
            output_fields=OUTPUT_FIELDS,
        )
        return [_row_to_candidate(h, channel="sparse", score=float(h.get("distance", 0.0)))
                for h in (res[0] if res else [])]

    def probe(self, expr: str) -> int:
        """统计满足条件的条数。"""
        rows = self._client.query(
            collection_name=self._collection,
            filter=expr,
            output_fields=["count(*)"],
        )
        if not rows:
            return 0
        return int(list(rows[0].values())[0])


class UnavailableVectorSearcher:
    """Milvus 不可用时的占位实现：显式报错，绝不返回空集冒充正常。"""

    def dense(self, vector: list[float], expr: str, limit: int) -> list[dict]:
        raise AppError("INDEX_NOT_ACTIVE", "Milvus 不可用，检索中止")

    def sparse(self, weights: dict[int, float], expr: str, limit: int) -> list[dict]:
        raise AppError("INDEX_NOT_ACTIVE", "Milvus 不可用，检索中止")

    def probe(self, expr: str) -> int:
        raise AppError("INDEX_NOT_ACTIVE", "Milvus 不可用，无法判断空集原因")


def build_searcher(settings: Settings) -> VectorSearcher:
    """连接 Milvus；失败时保留占位实现，避免服务无法启动。"""
    try:
        return MilvusVectorSearcher(settings)
    except Exception:
        return UnavailableVectorSearcher()
