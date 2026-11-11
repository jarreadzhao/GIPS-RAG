"""检索接口：API-15 /provisions/search 与 API-17 /retrieval/test。

ACL 只从网关请求头解析（见 app.retrieve.acl），查询参数与请求体里的
`acl_scope` 一律忽略，避免客户端伪造越权（AC-25）。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.errors import AppError
from app.retrieve.acl import (actor_role, assert_role_view_allowed, resolve_acl_scope,
                              scope_from_role)
from app.retrieve.search import Query as SearchQuery

router = APIRouter(tags=["retrieval"])


class RetrievalTestRequest(BaseModel):
    """召回测试台请求（接口 API-17）。"""

    query: str = Field(..., min_length=1, max_length=2000)
    role_view: str | None = None
    config: dict = Field(default_factory=dict)
    channels: list[str] = Field(default_factory=lambda: ["dense", "sparse", "sql"])
    top_k: int = Field(default=20, ge=1, le=100)


def _parse_channels(raw: str | None) -> tuple[str, ...]:
    """把逗号分隔的通道参数解析成元组。"""
    if not raw:
        return ("dense", "sparse", "sql")
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _payload(result) -> dict:
    """把 SearchResult 转成 CT-1 的响应体。"""
    return {
        "candidates": [c.as_dict() for c in result.candidates],
        "fused": [h.as_dict() for h in result.fused],
        "reranked": [h.as_dict() for h in result.reranked],
        "sparse_disabled": result.sparse_disabled,
        "empty_reason": result.empty_reason,
        "channel_errors": result.channel_errors,
        "rerank_skipped": result.rerank_skipped,
        "index_version": result.index_version,
        "query_lang": result.query_lang,
    }


@router.get("/provisions/search")
async def search_provisions(
    request: Request,
    q: str = Query(..., min_length=1, max_length=2000),
    channels: str | None = Query(default=None),
    top_k: int = Query(default=20, ge=1, le=100),
    corpus_type: str | None = Query(default=None),
    effective_at: str | None = Query(default=None),
    lang: str | None = Query(default=None),
) -> dict:
    """三通道检索（API-15）。"""
    ctx = request.app.state.ctx
    # 客户端把 acl_scope 塞进 query string 是伪造尝试，记录下来但绝不采用
    if "acl_scope" in request.query_params:
        get_logger("retrieval").warning(
            "acl_scope_forged_ignored", source="query_string",
            request_id=getattr(request.state, "request_id", ""))
    scope = resolve_acl_scope(request)
    if scope is None:
        raise AppError("ACL_SCOPE_MISSING",
                       "缺少网关注入的 acl_scope，拒绝检索")
    result = await ctx.retrieval.search(SearchQuery(
        text=q, channels=_parse_channels(channels), acl_scope=scope,
        top_k=top_k, corpus_type=corpus_type, effective_at=effective_at, lang=lang,
    ))
    body = _payload(result)
    body["request_id"] = getattr(request.state, "request_id", "")
    return body


@router.post("/retrieval/test")
async def retrieval_test(request: Request,
                         body: RetrievalTestRequest) -> dict:
    """召回测试台（API-17）。结果不写台账。"""
    ctx = request.app.state.ctx
    role = actor_role(request)
    assert_role_view_allowed(role, body.role_view)

    if body.role_view:
        dept = request.headers.get("X-Dept", "").strip()
        if not dept:
            raise AppError("ACL_SCOPE_MISSING", "role_view 模拟需要 X-Dept 指明部门")
        scope = scope_from_role(body.role_view, dept)
    else:
        scope = resolve_acl_scope(request)
        if scope is None:
            raise AppError("ACL_SCOPE_MISSING",
                           "缺少网关注入的 acl_scope，拒绝检索")

    started = time.perf_counter()
    result = await ctx.retrieval.search(SearchQuery(
        text=body.query, channels=tuple(body.channels), acl_scope=scope,
        top_k=body.top_k,
        corpus_type=(body.config or {}).get("corpus_type"),
        effective_at=(body.config or {}).get("effective_at"),
        lang=(body.config or {}).get("lang"),
        rerank=bool((body.config or {}).get("rerank", True)),
    ))
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    payload = _payload(result)
    # hits 按接口定义取条款级重排结果；config_echo 必须与输入逐字段一致（AC-23）
    payload["hits"] = payload["reranked"]
    payload["latency_ms"] = elapsed_ms
    payload["config_echo"] = body.config
    payload["acl_scope_used"] = scope
    payload["request_id"] = getattr(request.state, "request_id", "")
    return payload
