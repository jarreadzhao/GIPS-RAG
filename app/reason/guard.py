"""三道守卫：入模注入、数值硬隔离、出模效力性表述（AC-36、AC-45、AC-46、AC-47）。

数值规则（CT-3）：
    - 模型只能写 `calc://{calc_id}`，不得自己算出金额、比例、小数；
    - 白名单：条款编号、页码、年度、条款原文引用中的数字；
    - 任一带小数的数字、百分比、货币金额，一律 `CALC_NOT_ALLOWED`。
守卫只拒绝，不做四舍五入、不做单位换算、不替模型改写。
"""
from __future__ import annotations

import re
from typing import Any

from app.errors import AppError
from app.reason.prompts.cot_v1 import (EFFICACY_PHRASES, HINT_PHRASES,
                                       INJECTION_MARKERS)

DECIMAL_RE = re.compile(r"\d+\.\d+")
PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s*[%％]")
CURRENCY_RE = re.compile(r"[$¥€£￥]\s*\d")
CALC_RE = re.compile(r"calc://[A-Za-z0-9_\-:]+")
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
PROVISION_RE = re.compile(r"\b\d+\.[A-Z]\.\d+\b")

# 这些键装的是原件引用或结构位置，里面的数字天然合法
_SKIP_KEYS = {
    "provision_id", "page_no", "page_start", "page_end", "version_no",
    "char_start", "char_end", "report_year", "year", "span_text", "quote",
    "excerpt", "evidence_id", "ref_id",
}


def detect_injection(text: str) -> str | None:
    """返回命中的注入标记；未命中返回 None。"""
    lowered = (text or "").lower()
    for marker in INJECTION_MARKERS:
        if marker in lowered:
            return marker
    return None


def assert_no_injection(text: str) -> None:
    """入模前拒绝注入文本，判定路径不启动。"""
    marker = detect_injection(text)
    if marker:
        raise AppError("VALIDATION_FAILED",
                       f"输入含提示词注入标记：{marker}，已拒绝进入判定路径",
                       details={"marker": marker})


def scan_numbers(payload: Any) -> list[str]:
    """递归扫描结构化输出里的非法数字，返回违规清单。"""
    violations: list[str] = []
    _walk(payload, "", violations)
    return violations


def assert_numbers_allowed(payload: Any) -> None:
    """有违规数字即整条 finding 拒收，不返回部分结果。"""
    violations = scan_numbers(payload)
    if violations:
        raise AppError("CALC_NOT_ALLOWED",
                       "输出含未经 calc_results 支持的数值，拒绝落库",
                       details={"violations": violations[:10]})


def filter_efficacy(text: str) -> tuple[str, list[str]]:
    """把效力性表述替换为免责表述，返回 (过滤后文本, 命中词)。"""
    hits: list[str] = []
    filtered = text or ""
    for phrase in EFFICACY_PHRASES:
        if phrase in filtered:
            hits.append(phrase)
            filtered = filtered.replace(phrase, "【内部自查发现，非合规结论】")
    return filtered, hits


def find_hints(text: str) -> list[str]:
    """NOT_APPLIES 后禁止附带的提示性缺口（AC-42）。"""
    return [p for p in HINT_PHRASES if p in (text or "")]


def find_speculative(text: str) -> list[str]:
    """检索为空时禁止用先验知识补齐（AC-45）。"""
    lowered = (text or "").lower()
    return [p for p in ("通常", "一般来说", "行业惯例", "实践中多数机构",
                        "类似标准要求", "usually", "generally", "industry practice")
            if p in lowered]


def _walk(value: Any, path: str, out: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _SKIP_KEYS:
                continue
            _walk(item, f"{path}.{key}" if path else str(key), out)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _walk(item, f"{path}[{index}]", out)
        return
    if not isinstance(value, str):
        return

    text = value
    for token in CALC_RE.findall(text):
        text = text.replace(token, " ")
    for pid in PROVISION_RE.findall(text):
        # 条款编号属于白名单；引用其他条款编号也不代表数值来源合法，
        # 但编号本身不含小数/百分比，替换后不会掩盖真实违规。
        text = text.replace(pid, " ")
    text = YEAR_RE.sub(" ", text)

    for pattern, label in ((DECIMAL_RE, "decimal"), (PERCENT_RE, "percent"),
                           (CURRENCY_RE, "currency")):
        for hit in pattern.findall(text):
            out.append(f"{path or '<root>'}: {label} {hit!r}")
