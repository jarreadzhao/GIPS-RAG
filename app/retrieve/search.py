"""检索编排（CT-1 / PRD 5.5、5.6）。

一次检索的顺序固定：
    1. 校验 query 与 channels；
    2. 校验 acl_scope（缺失即拒绝，且此步之前不碰数据库）；
    3. 读取 active index_version；
    4. 三通道并行，单通道 800ms；单通道失败只记 channel_errors；
    5. RRF 融合；
    6. 条款级聚合 + 重排（1s 超时降级为 RRF 原序）；
    7. 空集归因 acl_filtered / no_match。
三通道全部失败才返回 504，绝不返回部分结果冒充完整结果。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.config import Settings
from app.core.db import Database
from app.core.embed import Embedder
from app.core.logging import get_logger
from app.core.rerank import Reranker
from app.errors import AppError
from app.index.builder import DISPLAY_ONLY_INDEX
from app.retrieve.acl import split_scope
from app.retrieve.fusion import group_by_provision, rrf_fuse
from app.retrieve.structured import StructuredChannel
from app.retrieve.types import Candidate, FusedHit, ProvisionHit, SearchResult
from app.retrieve.vector import VectorSearcher, build_acl_expr, build_filter

VALID_CHANNELS = ("dense", "sparse", "sql")


def detect_lang(text: str) -> str:
    """按汉字占比判定语种：≥30% 中文、≤5% 英文、其余 mixed。"""
    if not text:
        return "en"
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    ratio = cjk / len(text)
    if ratio >= 0.3:
        return "zh"
    if ratio <= 0.05:
        return "en"
    return "mixed"


@dataclass
class Query:
    """一次检索请求（接口层已解析成该结构）。"""

    text: str
    channels: tuple[str, ...] = VALID_CHANNELS
    acl_scope: list[str] | None = None
    top_k: int = 20
    corpus_type: str | None = None
    effective_at: str | None = None
    lang: str | None = None
    rerank: bool = True          # 召回测试台可关闭重排，用于对比 R@1/R@5


class RetrievalService:
    """三通道检索 + 融合 + 条款级重排。"""

    def __init__(self, db: Database, embedder: Embedder, reranker: Reranker,
                 searcher: VectorSearcher, settings: Settings,
                 structured: StructuredChannel | None = None) -> None:
        self._db = db
        self._embedder = embedder
        self._reranker = reranker
        self._searcher = searcher
        self._settings = settings
        self._structured = structured or StructuredChannel(
            settings.db_path, settings, db)
        self._log = get_logger("retrieval")
        # 冷启动标记：模型首次加载远慢于常规超时，加载成功后恢复 800ms/1s 限制
        self._embed_warmed = False
        self._rerank_warmed = False

    # ---------- 元数据 ----------
    def _active_index_versions(self) -> list[str]:
        """当前可检索的索引版本；没有 active 版本即 409。"""
        rows = self._db.fetch_all(
            "SELECT DISTINCT index_version FROM chunks "
            "WHERE status='active' AND index_version <> ?",
            (DISPLAY_ONLY_INDEX,),
        )
        versions = [r["index_version"] for r in rows if r["index_version"]]
        if not versions:
            raise AppError("INDEX_NOT_ACTIVE", "没有 active 索引版本，拒绝检索")
        return versions

    def _active_languages(self) -> set[str]:
        rows = self._db.fetch_all(
            "SELECT DISTINCT lang FROM chunks "
            "WHERE status='active' AND index_version <> ?",
            (DISPLAY_ONLY_INDEX,),
        )
        return {r["lang"] for r in rows if r["lang"]}

    def _effective_doc_ids(self, effective_at: str) -> list[str]:
        """按生效日筛出文档，转成 Milvus 的 doc_id 前置过滤。"""
        rows = self._db.fetch_all(
            "SELECT DISTINCT doc_id FROM documents WHERE status='published' "
            "AND index_status <> 'display_only' "
            "AND effective_from <= ? AND (effective_to IS NULL OR effective_to >= ?)",
            (effective_at, effective_at),
        )
        return [r["doc_id"] for r in rows]

    # ---------- 主流程 ----------
    async def search(self, query: Query) -> SearchResult:
        """执行一次完整检索。"""
        text = (query.text or "").strip()
        if not 1 <= len(text) <= 2000:
            raise AppError("VALIDATION_FAILED", "query 长度必须在 1 到 2000 字之间")

        channels = tuple(c for c in query.channels if c)
        if not channels:
            raise AppError("VALIDATION_FAILED", "channels 至少需要一个通道")
        unknown = [c for c in channels if c not in VALID_CHANNELS]
        if unknown:
            raise AppError("VALIDATION_FAILED", f"未知通道：{', '.join(unknown)}")
        top_k = max(1, min(int(query.top_k), 100))

        # AC-16：缺失 acl_scope 必须在任何查库动作之前拒绝
        if query.acl_scope is None:
            raise AppError("ACL_SCOPE_MISSING",
                           "缺少 acl_scope：网关注入缺失时拒绝检索，不得无过滤查全库")

        versions = self._active_index_versions()
        doc_ids = (self._effective_doc_ids(query.effective_at)
                   if query.effective_at else None)
        base_expr = build_filter(versions, corpus_type=query.corpus_type,
                                 lang=query.lang, doc_ids=doc_ids)
        depts, clearances = split_scope(query.acl_scope)
        acl_expr = build_acl_expr(base_expr, depts, clearances)
        query_lang = detect_lang(text)

        # 空 scope（或只有未知标签）只可能匹配公开件；当前语料没有公开标记，
        # 因此直接返回空集，并明确归因为权限问题
        if acl_expr is None:
            return SearchResult(empty_reason="acl_filtered",
                                index_version=",".join(versions),
                                query_lang=query_lang)

        index_langs = self._active_languages()
        sparse_disabled = ""
        if "sparse" in channels:
            if (query_lang in ("zh", "en") and index_langs
                    and query_lang not in index_langs):
                sparse_disabled = "lang_mismatch"
                self._log.info("sparse_disabled", reason="lang_mismatch",
                               query_lang=query_lang, index_langs=sorted(index_langs))

        timeout = self._settings.retrieval_channel_timeout_ms / 1000
        embed_timeout = (timeout if self._embed_warmed
                         else self._settings.retrieval_warmup_timeout_s)
        channel_results: list[tuple[str, list[Candidate]]] = []
        channel_errors: dict[str, str] = {}
        # 实际尝试的通道集合；嵌入失败会把依赖它的通道一并记为失败
        channel_attempts: set[str] = set()

        # 嵌入只做一次：稠密与稀疏共用同一次 bge-m3 编码，避免两次占卡
        embedding = None
        needs_embed = "dense" in channels or (
            "sparse" in channels and not sparse_disabled)
        if needs_embed:
            try:
                embedding = await asyncio.wait_for(
                    asyncio.to_thread(self._embedder.encode, [text]),
                    timeout=embed_timeout)
                self._embed_warmed = True
            except asyncio.TimeoutError:
                channel_errors["embed"] = f"timeout>{int(embed_timeout * 1000)}ms"
            except Exception as exc:  # 模型不可用时明确记录，不静默返回空集
                channel_errors["embed"] = str(exc)[:200]

        tasks: dict[str, asyncio.Task] = {}
        if "dense" in channels:
            channel_attempts.add("dense")
            if embedding is None:
                channel_errors["dense"] = channel_errors.get("embed", "embed failed")
            else:
                tasks["dense"] = asyncio.create_task(asyncio.wait_for(
                    asyncio.to_thread(self._searcher.dense,
                                      embedding.dense[0], acl_expr, top_k),
                    timeout=timeout))
        if "sparse" in channels and not sparse_disabled:
            channel_attempts.add("sparse")
            if embedding is None:
                channel_errors["sparse"] = channel_errors.get("embed", "embed failed")
            else:
                tasks["sparse"] = asyncio.create_task(asyncio.wait_for(
                    asyncio.to_thread(self._searcher.sparse,
                                      embedding.sparse[0], acl_expr, top_k),
                    timeout=timeout))
        if "sql" in channels:
            channel_attempts.add("sql")
            tasks["sql"] = asyncio.create_task(asyncio.wait_for(
                asyncio.to_thread(self._structured.search, text,
                                  index_versions=versions,
                                  depts=depts, clearances=clearances),
                timeout=self._settings.retrieval_sql_timeout_s))

        for name, task in tasks.items():
            try:
                channel_results.append((name, await task))
            except asyncio.TimeoutError:
                channel_errors[name] = (
                    f"timeout>{int(self._settings.retrieval_sql_timeout_s * 1000)}ms"
                    if name == "sql" else f"timeout>{int(timeout * 1000)}ms")
                self._log.warning("retrieval_channel_timeout", channel=name)
            except Exception as exc:
                channel_errors[name] = str(exc)[:200]
                self._log.warning("retrieval_channel_failed", channel=name,
                                  reason=str(exc)[:200])

        # 所有实际尝试的通道都失败才 504；单通道失败不整体失败
        failed_channels = set(channel_errors) - {"embed"}
        if channel_attempts and not channel_results and channel_attempts <= failed_channels:
            raise AppError("UPSTREAM_TIMEOUT", "检索通道全部失败或超时，拒绝返回部分结果",
                           details={"channel_errors": channel_errors})

        ordered = [(name, items) for name, items in
                   sorted(channel_results, key=lambda p: VALID_CHANNELS.index(p[0]))]
        fused = rrf_fuse(ordered, k=self._settings.retrieval_rrf_k)
        candidates = [c for _name, items in ordered for c in items]

        result = SearchResult(
            candidates=candidates, fused=fused,
            sparse_disabled=sparse_disabled, channel_errors=channel_errors,
            index_version=",".join(versions), query_lang=query_lang,
        )
        if not fused:
            # 只有向量通道真正跑成功，空集归因才可信；
            # 否则「查不到」可能是模型没加载或通道超时，不能甩锅给 ACL
            vector_ok = any(name in ("dense", "sparse") for name, _ in channel_results)
            result.empty_reason = (
                await self._empty_reason(base_expr, acl_expr) if vector_ok else "no_match")
            return result

        groups = self._clause_groups(group_by_provision(fused))
        if not query.rerank:
            # 测试台显式关闭重排：直接给 RRF 原序，便于对比重排前后的 R@1/R@5
            result.reranked = self._fallback(
                groups, self._settings.retrieval_rerank_top_provisions)
            result.rerank_skipped = True
            return result

        result.reranked, skipped = await self._rerank(text, groups)
        result.rerank_skipped = skipped
        if skipped:
            self._log.warning("rerank_skipped", query_lang=query_lang,
                              groups=len(groups))
        return result

    # ---------- 空集归因 ----------
    @staticmethod
    def _clause_groups(groups: list[tuple[str, list[FusedHit]]]
                       ) -> list[tuple[str, list[FusedHit]]]:
        """优先只保留有条款号的组（D-M2-07）。

        术语表、附录没有条款编号，不能顶着条款身份占用引用列表的槽位，
        否则审查员会看到「第 8.H.4 条」实际是附录样例。若所有组都没有
        条款号（例如问术语定义），仍退回全集，避免直接空答。
        """
        clause = [g for g in groups if not g[0].startswith("chunk:")]
        return clause or groups

    async def _empty_reason(self, base_expr: str, acl_expr: str) -> str:
        """用去掉 ACL 的探测查询区分「权限筛空」与「本来就没有」。"""
        try:
            count = await asyncio.wait_for(
                asyncio.to_thread(self._searcher.probe, base_expr),
                timeout=self._settings.retrieval_channel_timeout_ms / 1000)
        except Exception:
            return "no_match"
        return "acl_filtered" if count > 0 else "no_match"

    # ---------- 重排 ----------
    async def _rerank(self, query: str,
                      groups: list[tuple[str, list[FusedHit]]]
                      ) -> tuple[list[ProvisionHit], bool]:
        """条款级重排；超时或模型失败时降级为 RRF 原序并标 rerank_skipped。"""
        limit = self._settings.retrieval_rerank_top_provisions
        if not groups:
            return [], False
        rerank_timeout = (self._settings.retrieval_rerank_timeout_s
                          if self._rerank_warmed
                          else self._settings.retrieval_warmup_timeout_s)
        try:
            hits = await asyncio.wait_for(
                asyncio.to_thread(self._score_groups, query, groups, limit),
                timeout=rerank_timeout)
            self._rerank_warmed = True
            return hits, False
        except asyncio.TimeoutError:
            return self._fallback(groups, limit), True
        except Exception as exc:
            self._log.warning("rerank_failed", reason=str(exc)[:200])
            return self._fallback(groups, limit), True

    def _score_groups(self, query: str, groups: list[tuple[str, list[FusedHit]]],
                      limit: int) -> list[ProvisionHit]:
        """每个条款取前 3 个 chunk 拼成代表文本，再交给重排模型打分。"""
        reps: list[str] = []
        for _key, chunks in groups:
            joined = "\n".join(c.text for c in chunks[:3])
            reps.append(joined[:1200])
        scores = self._reranker.score(query, reps)
        if len(scores) != len(groups):
            raise ValueError(f"重排返回条数不一致：{len(scores)} != {len(groups)}")

        ranked = sorted(zip(groups, scores), key=lambda p: -float(p[1]))
        out: list[ProvisionHit] = []
        seen: set[str] = set()
        for (key, chunks), score in ranked:
            provision = key if not key.startswith("chunk:") else (chunks[0].provision_id or key)
            if provision in seen:
                continue
            seen.add(provision)
            out.append(ProvisionHit(provision_id=provision, score=float(score),
                                    rank=len(out) + 1, chunks=chunks[:3]))
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _fallback(groups: list[tuple[str, list[FusedHit]]], limit: int
                  ) -> list[ProvisionHit]:
        """RRF 原序兜底；同一条款仍然只占一席。"""
        out: list[ProvisionHit] = []
        for key, chunks in groups:
            provision = key if not key.startswith("chunk:") else (chunks[0].provision_id or key)
            out.append(ProvisionHit(provision_id=provision,
                                    score=chunks[0].rrf_score,
                                    rank=len(out) + 1, chunks=chunks[:3]))
            if len(out) >= limit:
                break
        return out
