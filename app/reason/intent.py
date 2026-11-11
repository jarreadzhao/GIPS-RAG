"""意图三出口与查询改写（T-2-01 前置，AC-29～AC-33）。

三个出口必须改变路径，否则识别就是空转：
    knowledge -> 改写 -> 检索
    media     -> ASR / OCR / VL -> 改写 -> 检索
    chitchat  -> 直接回复，不检索、不调生成模型

改写只做三件事：指代消解、缩写/术语归一、中英术语映射。
**不改写条款编号**：输入里出现的 `1.A.4` 必须在改写结果里原样保留。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.logging import get_logger

PROVISION_RE = re.compile(r"\b\d+\.[A-Z]\.\d+\b")

GREETINGS = {"你好", "您好", "hi", "hello", "在吗", "谢谢", "thanks", "thank you"}

KNOWLEDGE_HINTS = (
    "gips", "条款", "制度", "政策", "程序", "合规", "记录", "披露", "保存",
    "组合", "composite", "policy", "procedure", "standard", "requirement",
    "record", "disclosure", "compliance",
)

ANAPHORS = ("这条", "该条", "这个条款", "该条款", "它", "this requirement",
            "this provision", "that provision", "that requirement")

# 术语归一：只做等价展开，不引入原文没有的限定词
TERM_MAP = {
    "GIPS": "GIPS 2020",
    "P&P": "policies and procedures",
    "policies and procedures": "policies and procedures",
    "composite": "composite",
}

MEDIA_ROUTES = {
    "audio": "asr",
    "image": "vl",
    "scan": "ocr",
    "scan_image": "ocr",
}


@dataclass
class IntentResult:
    """意图识别结果。"""

    intent: str                 # knowledge / media / chitchat
    media_route: str = ""       # asr / ocr / vl
    reason: str = ""


@dataclass
class RewriteResult:
    """改写结果；expansions 单独记录，便于回放定位召回偏移。"""

    text: str
    expansions: list[str] = field(default_factory=list)
    preserved_ids: list[str] = field(default_factory=list)


def classify_input(text: str, media: list[str] | None = None) -> IntentResult:
    """按媒体、问候语、实体/术语线索判定三出口。"""
    text = (text or "").strip()
    media = media or []
    if media:
        route = MEDIA_ROUTES.get(media[0], "vl")
        # AC-30：分支必须落日志，不能只在代码里 if 一下；
        # 频道只能是 ingest/retrieval/generate/verify/ledger/access 之一。
        get_logger("retrieval").info("media_route", media=media, route=route)
        return IntentResult(intent="media", media_route=route, reason="含媒体输入")

    lowered = text.lower()
    if lowered in GREETINGS or (len(text) <= 6 and any(
            lowered.startswith(g) for g in GREETINGS)):
        return IntentResult(intent="chitchat", reason="问候/无实体")
    if not text:
        return IntentResult(intent="chitchat", reason="空输入")

    # 兜底走知识问答：只有明确问候才不检索。
    # 关键词表无法穷举（「What must a firm disclose about fees?」不含表内词），
    # 误判成 chitchat 会把本可回答的问题静默换成一句问候，代价高于一次空检索。
    if PROVISION_RE.search(text) or any(h in lowered for h in KNOWLEDGE_HINTS):
        return IntentResult(intent="knowledge", reason="含条款编号或合规实体")
    return IntentResult(intent="knowledge", reason="非问候语，按知识问答处理")


def rewrite_query(text: str, history: list[dict] | None = None) -> RewriteResult:
    """指代消解 + 术语归一；条款编号原样保留。"""
    original = (text or "").strip()
    preserved = PROVISION_RE.findall(original)
    expansions: list[str] = []
    rewritten = original

    # 指代消解：只有历史里确实有 provision_id 时才补，不猜
    lowered = rewritten.lower()
    if any(a in rewritten or a in lowered for a in ANAPHORS):
        last_id = _last_provision_id(history or [])
        if last_id and last_id not in rewritten:
            rewritten = f"{last_id} {rewritten}"
            expansions.append(f"anaphora->{last_id}")

    # 术语归一：长词优先，避免 GIPS 先被替换后 P&P 失配
    for term in sorted(TERM_MAP, key=len, reverse=True):
        if term in rewritten and TERM_MAP[term] != term:
            rewritten = rewritten.replace(term, TERM_MAP[term])
            expansions.append(f"{term}->{TERM_MAP[term]}")

    # 编号保护：替换过程若碰到编号，必须恢复原文编号
    for pid in preserved:
        if pid not in rewritten:
            rewritten = f"{pid} {rewritten}"

    return RewriteResult(text=rewritten, expansions=expansions,
                         preserved_ids=preserved)


def _last_provision_id(history: list[dict]) -> str:
    """取最近一轮的 provision_id。"""
    for item in reversed(history):
        pid = (item or {}).get("provision_id")
        if pid:
            return str(pid)
    return ""
