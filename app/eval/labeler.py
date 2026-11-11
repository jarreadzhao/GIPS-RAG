"""AI 初标器：对同一问题跑 3 次，取多数并计算自一致率。

不把「模型说了什么」当事实：输出必须能被 JSON 解析且条款编号出现在候选里，
否则该次记为无效。三次全无效的条目进入人工必检（confidence=0）。
"""
from __future__ import annotations

import hashlib
import json
import re

PROMPT_VERSION = "labeler-v1"
_PROMPT_TEMPLATE = (
    "你是 GIPS 2020 合规语料标注员。给定问题与候选条款，"
    "只输出一个 JSON 数组，元素是命中条款编号（形如 1.A.4），按相关性从高到低排序。"
    "不允许输出解释、Markdown 或多余文字。若均不相关输出 []。\n"
    "问题：{query}\n候选：{candidates}\n"
)


def prompt_hash() -> str:
    """提示词版本哈希，写入每条的 labeler_prompt_hash。"""
    return "sha256:" + hashlib.sha256(
        f"{PROMPT_VERSION}|{_PROMPT_TEMPLATE}".encode("utf-8")).hexdigest()


def build_prompt(query: str, candidates: list[dict]) -> str:
    """候选只给编号与 200 字摘要，避免把整篇语料塞进上下文。"""
    view = [{"provision_id": c.get("provision_id"),
             "snippet": str(c.get("text") or "")[:200]} for c in candidates]
    return _PROMPT_TEMPLATE.format(
        query=query, candidates=json.dumps(view, ensure_ascii=False))


def parse_labels(text: str, allowed: set[str]) -> list[str]:
    """从模型输出里抽取合法条款编号；非法编号直接丢弃。"""
    match = re.search(r"\[[^\]]*\]", text or "", re.S)
    if not match:
        return []
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        pid = str(item).strip().strip('"')
        if pid in allowed and pid not in out:
            out.append(pid)
    return out


def label_once(llm, query: str, candidates: list[dict]) -> tuple[list[str], str]:
    """执行一次标注，同时回传模型指纹。"""
    from app.core.llm import LLMRequest

    allowed = {str(c.get("provision_id")) for c in candidates if c.get("provision_id")}
    prompt = build_prompt(query, candidates)
    result = llm.generate(LLMRequest(prompt=prompt, max_tokens=200, temperature=0.0))
    return parse_labels(result.text, allowed), result.model_fingerprint


def label_query(llm, query: str, candidates: list[dict], *, runs: int = 3) -> dict:
    """多数投票 + 自一致率。

    self_consistency = 出现次数最多的答案占比（1/3、2/3 或 1）。
    confidence 取 self_consistency；低于 0.70 的条目会被抽检规则强制命中。
    """
    votes: list[list[str]] = []
    fingerprints: list[str] = []
    for _ in range(runs):
        labels, fingerprint = label_once(llm, query, candidates)
        votes.append(labels)
        fingerprints.append(fingerprint)
    serialized = [json.dumps(v, ensure_ascii=False) for v in votes]
    counts: dict[str, int] = {}
    for key in serialized:
        counts[key] = counts.get(key, 0) + 1
    best_key, best_count = max(counts.items(), key=lambda kv: kv[1])
    consistency = round(best_count / runs, 4)
    return {
        "gold_provisions": json.loads(best_key),
        "self_consistency": consistency,
        "confidence": consistency,
        "labeler_model": fingerprints[0] if fingerprints else "unknown",
        "labeler_prompt_hash": prompt_hash(),
        "runs": runs,
    }
