"""清洗与去噪（T-0-11）。

边界（PRD 4.3）：只做「删整块」，不做「改文字」。
允许删：页眉页脚、页码、水印、参考文献段、孤立字符、空白块。
禁止做：空白归一、连字符拼接、全半角转换、表格重构、改写「注/例外」。
以上任何一条都会让 span_text 与原件的字节不一致，回验器直接判失败。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace

from app.parse.pdf_text import PageTextBlock, ParsedDocument, rebuild_page_stream

CLEANER_VERSION = "cleaner-v1"

# 参考文献起始标志：命中后本页及后续页不再进入语料
REFERENCE_RE = re.compile(r"^\s*(References|REFERENCES|参考文献|Bibliography)\s*$")
# 页眉页脚常见形态：公司名 + 版本号 + 页码 + 日期
HEADER_FOOTER_RE = re.compile(
    r"(第\s*\d+\s*页|Page\s+\d+\s*(of|/)\s*\d+|^\s*\d+\s*/\s*\d+\s*$|"
    r"(Confidential|Internal Use Only|内部资料)\s*$)",
    re.I,
)
# 「注/例外」段落：即便很短也必须保留，它们是判定 PARTIAL 的关键依据
NOTE_RE = re.compile(r"^\s*(注|例外|除外|不适用|Note|Exception|Notwithstanding)\b", re.I)


@dataclass
class CleanedDocument:
    """清洗后的文档块，保持原始顺序与偏移。"""

    blocks: list[PageTextBlock] = field(default_factory=list)
    # 与 blocks 严格对应的页内规范字符流：删块后必须重建，
    # 否则回验切片会把已删的页眉/参考文献算进 span
    pages_text: list[str] = field(default_factory=list)
    dropped: Counter = field(default_factory=Counter)   # 各类丢弃计数，进质量报告
    version: str = CLEANER_VERSION
    stopped_at_page: int | None = None                  # 命中参考文献的页码


def _norm_for_repeat(text: str) -> str:
    """把页码与日期替换成占位符，用于识别「跨页重复出现的页眉」。"""
    t = re.sub(r"\d+", "#", text)
    return re.sub(r"\s+", " ", t).strip().lower()


def clean(parsed: ParsedDocument, *, version: str = CLEANER_VERSION) -> CleanedDocument:
    """按版本化规则删块。"""
    blocks = parsed.blocks
    if not blocks:
        return CleanedDocument(blocks=[], pages_text=[], version=version)

    # 1) 跨页重复行：同一模板出现在 ≥30% 的页首/页尾，判为页眉页脚
    page_count = max(1, len({b.page_no for b in blocks}))
    first_of_page = {}
    for b in blocks:
        first_of_page.setdefault(b.page_no, set()).add(_norm_for_repeat(b.text))
    repeat_counter = Counter()
    for texts in first_of_page.values():
        for t in texts:
            repeat_counter[t] += 1
    repeated = {t for t, c in repeat_counter.items()
                if c >= max(3, int(page_count * 0.3)) and len(t) <= 80}

    out: list[PageTextBlock] = []
    dropped: Counter = Counter()
    stopped: int | None = None
    for b in blocks:
        if stopped is not None:
            dropped["after_references"] += 1
            continue
        text = b.text.strip()
        if REFERENCE_RE.match(text):
            stopped = b.page_no
            dropped["references"] += 1
            continue
        if _norm_for_repeat(text) in repeated and not NOTE_RE.match(text):
            dropped["repeated_header"] += 1
            continue
        if HEADER_FOOTER_RE.search(text):
            dropped["header_footer"] += 1
            continue
        # 孤立字符：单字符且不是编号/注记，通常是水印或印章碎片
        if len(text) == 1 and not NOTE_RE.match(text):
            dropped["isolated_char"] += 1
            continue
        # 复制一份再改偏移，避免把调用方手里的原始块也改掉
        out.append(replace(b))

    total_pages = (parsed.report.page_count if parsed.report
                   else max(b.page_no for b in blocks))
    return CleanedDocument(blocks=out,
                           pages_text=rebuild_page_stream(out, total_pages),
                           dropped=dropped, version=version,
                           stopped_at_page=stopped)
