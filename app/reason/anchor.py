"""锚点结构校验（T-2-04 / AC-35）。

只做结构校验，不做内容回验——内容回验在 verifier 里，且不得调用模型。
锚点缺字段时直接 `ANCHOR_INVALID`，不允许「先写半条链再补」。
"""
from __future__ import annotations

from typing import Any

from app.errors import AppError


def validate_step_anchors(step_type: str, anchors: list[dict[str, Any]] | None) -> None:
    """每一步至少要有一个锚点；缺字段即整链拒收。"""
    if not anchors:
        raise AppError("ANCHOR_INVALID", f"{step_type} 步缺少 anchors，拒绝进入回验")

    for anchor in anchors:
        kind = anchor.get("type")
        if kind == "provision":
            _require(anchor, ("provision_id",), step_type)
        elif kind == "evidence":
            _require(anchor, ("doc_id", "version_no", "page_no",
                              "char_start", "char_end", "span_text"), step_type)
            _check_range(anchor, step_type)
        elif kind == "comparison":
            _require(anchor, ("comparison_id",), step_type)
        else:
            raise AppError("ANCHOR_INVALID",
                           f"{step_type} 步存在未知锚点类型：{kind!r}")


def validate_evidence_refs(refs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """E 步证据引用的结构校验，返回规范化后的列表。"""
    if not refs:
        raise AppError("ANCHOR_INVALID", "E 步没有任何证据引用，拒绝进入回验")

    out: list[dict[str, Any]] = []
    for ref in refs:
        _require(ref, ("doc_id", "version_no", "page_no",
                       "char_start", "char_end", "span_text"), "E")
        _check_range(ref, "E")
        if not str(ref["span_text"]):
            raise AppError("ANCHOR_INVALID", "E 步 span_text 为空")
        out.append(ref)
    return out


def _require(anchor: dict[str, Any], keys: tuple[str, ...], step: str) -> None:
    missing = [k for k in keys if anchor.get(k) in (None, "")]
    if missing:
        raise AppError("ANCHOR_INVALID",
                       f"{step} 步锚点缺少字段：{', '.join(missing)}",
                       details={"missing": missing})


def _check_range(anchor: dict[str, Any], step: str) -> None:
    start, end = int(anchor["char_start"]), int(anchor["char_end"])
    if start < 0 or end <= start:
        raise AppError("ANCHOR_INVALID",
                       f"{step} 步码点区间非法：[{start}, {end})")
