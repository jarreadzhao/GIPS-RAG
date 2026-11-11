"""审查任务接口（API-4 / API-5 / API-8，M2 同步版）。

M2 只做单条款闭环，任务是同步完成的；`202` 仍按契约返回，客户端用
`GET /reviews/{task_id}` 或 `GET /cot/{finding_id}` 取结果，后续换异步队列
时响应形状不变。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.errors import AppError
from app.reason.orchestrator import ReviewRequest
from app.retrieve.acl import actor_role, resolve_acl_scope

router = APIRouter(tags=["reviews"])

FORMAL_ROLES = {"reviewer", "librarian", "approver", "admin"}
COT_ROLES = {"reviewer", "librarian", "approver", "admin"}


class ReviewBody(BaseModel):
    """API-4 请求体；`acl_scope` 只用于识别伪造，不参与鉴权。"""

    task_type: str = Field(default="formal", pattern="^(qa|formal)$")
    scope: list[str] = Field(default_factory=list, max_length=50)
    supply: str = "self_hosted"
    profile_version: int | None = None
    text: str = ""
    media: list[str] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)
    report_year: int | None = None
    # 答案语种：zh（默认）只回中文片段与中文模板，en 只回英文
    answer_lang: str = "zh"
    inference_scope: str | None = None
    acl_scope: Any = None


@router.post("/reviews")
async def create_review(
    request: Request,
    body: ReviewBody,
    idempotency_key: str | None = Header(default=None,
                                         alias="Idempotency-Key"),
) -> JSONResponse:
    """创建审查任务。"""
    ctx = request.app.state.ctx
    scope = resolve_acl_scope(request)
    if scope is None:
        raise AppError("ACL_SCOPE_MISSING", "缺少网关注入的 acl_scope")
    role = actor_role(request)
    if body.task_type == "formal" and role not in FORMAL_ROLES:
        raise AppError("ACL_DENIED", f"角色 {role or '(未提供)'} 无权发起正式审查")
    if body.acl_scope is not None:
        # AC-25 / AC-47D：客户端伪造的身份字段一律忽略并留痕
        get_logger("access").warning("acl_scope_forged_ignored", actor=role)
    if not idempotency_key:
        raise AppError("VALIDATION_FAILED", "缺少 Idempotency-Key 请求头")
    if body.answer_lang not in ("zh", "en"):
        raise AppError("VALIDATION_FAILED", "answer_lang 只支持 zh / en")

    req = ReviewRequest(
        task_type=body.task_type, scope=list(body.scope), supply=body.supply,
        profile_version=body.profile_version, text=body.text,
        media=list(body.media), history=list(body.history),
        report_year=body.report_year,
        answer_lang=body.answer_lang,
        claimed_inference_scope=body.inference_scope,
        acl_scope=scope, actor=role, idem_key=idempotency_key,
    )
    outcome = await ctx.review.run(req)
    payload = asdict(outcome)
    payload["acl_scope_used"] = scope
    return JSONResponse(status_code=200 if outcome.replayed else 202,
                        content=payload)


@router.get("/reviews/{task_id}")
async def get_review(task_id: str, request: Request) -> dict:
    """任务状态与 findings；失败任务的链也能看到（AC-40）。"""
    ctx = request.app.state.ctx
    task = ctx.db.fetch_one("SELECT * FROM review_tasks WHERE task_id=?",
                            (task_id,))
    if task is None:
        raise AppError("VALIDATION_FAILED", f"任务 {task_id} 不存在")
    findings = ctx.db.fetch_all(
        "SELECT * FROM findings WHERE task_id=? ORDER BY finding_id", (task_id,))
    steps = ctx.db.fetch_all(
        "SELECT chain_id, finding_id, step_no, step_type, payload_json, "
        "anchors_json, validator_result, self_hash FROM cot_chains "
        "WHERE task_id=? ORDER BY step_no", (task_id,))
    decoded = [_decode_step(s) for s in steps]
    payload = {
        "task": dict(task),
        "findings": [dict(f) for f in findings],
        "steps": decoded,
        # AC-71A：排队位次必须可观测。0=无人排队，>0=前面还有几个线程在等 GPU。
        # 无显存池（如测试注入的精简 context）时返回 None，不抛错。
        "queue_position": _queue_position(ctx, task),
    }
    if task["task_type"] == "ingest":
        # 入库任务复用 review_tasks；把文档状态一并带出，调用方不必再查一次
        payload["document"] = _ingest_document(ctx, task)
    if task["task_type"] == "qa":
        # QA 没有 finding，引用挂在 E 步 anchors 上；单独列一份便于前端渲染
        payload["citations"] = [
            anchor for step in decoded if step.get("step_type") == "E"
            for anchor in (step.get("anchors") or [])
            if isinstance(anchor, dict)]
    return payload


@router.get("/cot/{finding_id}")
async def get_cot(finding_id: str, request: Request) -> dict:
    """返回一条 finding 的完整 L-A-E-C-J 链（API-8）。"""
    role = actor_role(request)
    if role not in COT_ROLES:
        raise AppError("ACL_DENIED", f"角色 {role or '(未提供)'} 无权查看思维链")
    ctx = request.app.state.ctx
    finding = ctx.db.fetch_one("SELECT * FROM findings WHERE finding_id=?",
                               (finding_id,))
    if finding is None:
        raise AppError("VALIDATION_FAILED", f"finding {finding_id} 不存在")
    rows = ctx.db.fetch_all(
        "SELECT chain_id, step_no, step_type, payload_json, anchors_json, "
        "validator_result, self_hash FROM cot_chains WHERE finding_id=? "
        "ORDER BY step_no", (finding_id,))
    return {"finding_id": finding_id,
            "provision_id": finding["provision_id"],
            "steps": [_decode_step(r) for r in rows]}


def _queue_position(ctx: Any, task: Any) -> int | None:
    """读取当前排队位次；池不存在或读取失败时返回 None。

    ingest 任务排的是入库写队列，不是 GPU 队列，两者不能混用一个计数。
    """
    if task["task_type"] == "ingest":
        worker = getattr(ctx, "ingest", None)
        if worker is None:
            return None
        try:
            return int(worker.pending())
        except Exception:  # pragma: no cover - 队列实现异常不该拖垮状态查询
            return None
    pool = getattr(ctx, "pool", None)
    if pool is None:
        return None
    try:
        return int(pool.queue_position())
    except Exception:  # pragma: no cover - 池实现异常不该拖垮状态查询
        return None


def _ingest_document(ctx: Any, task: Any) -> dict | None:
    """按 ingest 任务的 scope_json 反查文档行；scope 损坏时返回 None。"""
    try:
        scope = json.loads(task["scope_json"] or "{}")
        doc_id = scope["doc_id"]
        version_no = int(scope["version_no"])
    except (KeyError, TypeError, ValueError):
        return None
    row = ctx.db.fetch_one(
        "SELECT doc_id, version_no, status, index_status, index_version, "
        "active_version, parser, page_count, text_layer_ratio FROM documents "
        "WHERE doc_id=? AND version_no=?", (doc_id, version_no))
    return dict(row) if row is not None else None


def _decode_step(row: Any) -> dict:
    """把落库的 JSON 文本还原为对象，非法 JSON 不掩盖。"""
    item = dict(row)
    for key in ("payload_json", "anchors_json"):
        target = key.replace("_json", "")
        try:
            item[target] = json.loads(item.pop(key))
        except (TypeError, json.JSONDecodeError):
            item[target] = item.pop(key)
    return item
