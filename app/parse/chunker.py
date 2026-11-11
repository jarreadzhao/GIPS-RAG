"""切分（T-0-11，由 src/chunk.py 改造）。

策略清单（PRD 4.4）：provision / section / paragraph / table_cell / semantic。
fixed_length 被配置层拒绝，本模块再校验一次，防止绕过配置直接调用。

两条不变量：
    1. 任何两个 chunk 不重叠（overlap 必须为 0）。
    2. chunk 不跨页：char_start/char_end 始终是「页内码点偏移」，
       跨页合并会让偏移失去唯一解释，回验时无法定位。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.parse.pdf_text import PageTextBlock, ParsedDocument

# GIPS 条款编号：1.A.4 / 3.B.2 / 5.A.12
PROVISION_RE = re.compile(r"^\s*第?\s*(\d{1,2}\.[A-Z]\.\d{1,3})\b")
# 章/节标题行：章标题为全大写（`2. INPUT DATA...`），节标题为 `1.A. Fundamentals`；
# 条款行 `1.A.1 ...` 与其他编号正文不会命中
HEADING_RE = re.compile(
    r"^\s*\d{1,2}\.\s+[A-Z][A-Z0-9 ,&/()\-]{6,}"
    r"|^\s*\d{1,2}\.[A-Z]\.?[\s\x07]+[A-Z]"
    # 中文对照版：章 `1. 合规的基础`、节 `1.A. 合规的基础 — 规定`
    r"|^\s*\d{1,2}\.\s*[\u4e00-\u9fff]"
    r"|^\s*\d{1,2}\.[A-Z]\.?[\s\x07]*[\u4e00-\u9fff]"
)
# 章节标题：数字编号 + 短标题，或全大写标题
SECTION_RE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,2})\s+[A-Z\u4e00-\u9fff]")
# 后置正文（术语表 / 附录 / 索引 / 参考书目）没有条款编号。
# 遇到这类全大写标题必须断开条款号延续，否则其后所有页面都会挂到文档最后
# 一条条款下（D-M2-07：附录 193 个 chunk 被误标为 8.H.4）。
# 中文译本同样有后置正文，标题不识别会把整份术语表挂到最后一条条款下
# （D-M2-07 的中文侧：103 个术语表 chunk 被误标为 8.H.4）。
# 中文标题限定「整行只有标题词 (+可选编号)」，避免正文里出现「附录…」就误判。
BACK_MATTER_RE = re.compile(
    r"^\s*(?:GLOSSARY|APPENDIX(?:ES)?|ANNEX(?:ES)?|INDEX|BIBLIOGRAPHY)\b"
    r"|^\s*(?:术语表|词汇表|附录|索引|参考书目)\s*[A-Za-z0-9一二三四五六七八九十]*"
    r"\s*(?:\n|$)")
SENTENCE_END = tuple("。！？；…!?.;")


@dataclass
class ChunkDraft:
    """切分产物，尚未分配 chunk_id 与索引版本。"""

    text: str
    chunk_type: str
    page_no: int
    char_start: int
    char_end: int
    provision_id: str | None = None
    lang: str = "en"
    pair_id: str | None = None


def validate_chunk_config(strategy: str, overlap: int) -> None:
    """切分配置二次校验：与 config 的 model_validator 互为兜底。"""
    if overlap != 0:
        raise ValueError(
            f"禁止「固定长度 + 重叠」切分：overlap={overlap} 会让同一片段落入两个 chunk，"
            "回验器无法判断原始锚点。"
        )
    if strategy in ("fixed_length", "fixed_length_overlap", "sliding_window"):
        raise ValueError(f"禁止的切分策略 {strategy}，请在 provision/section/paragraph 中选择。")


def _emit(page: int, blocks: list[PageTextBlock], chunk_type: str,
          provision_id: str | None, lang: str, target: int, hard_max: int
          ) -> list[ChunkDraft]:
    """把一组块按目标长度装进 chunk，只在块边界断开，绝不重叠。"""
    out: list[ChunkDraft] = []
    buf: list[PageTextBlock] = []
    size = 0

    def flush() -> None:
        nonlocal buf, size
        if not buf:
            return
        # 拼接分隔符由偏移决定：相邻块之间隔了换行就用 \n，同一块内切出的
        # 句子之间偏移连续（gap=0）就不加分隔符，否则 chunk.text 无法与
        # 页内字符流的切片逐字相等（semantic 策略曾因此只有 0.34 可回链）
        parts: list[str] = []
        prev_end: int | None = None
        for b in buf:
            if prev_end is not None:
                parts.append("" if b.char_start == prev_end else "\n")
            parts.append(b.text)
            prev_end = b.char_end
        out.append(ChunkDraft(
            text="".join(parts),
            chunk_type=chunk_type,
            page_no=page,
            char_start=min(b.char_start for b in buf),
            char_end=max(b.char_end for b in buf),
            provision_id=provision_id,
            lang=lang,
        ))
        buf, size = [], 0

    for b in blocks:
        n = len(b.text)
        if buf and (size >= target or size + n > hard_max):
            flush()
        buf.append(b)
        size += n + 1  # +1 为换行
    flush()
    return out


def _chunk_provision(blocks: list[PageTextBlock], lang: str,
                     target: int, hard_max: int,
                     carry: str | None = None) -> tuple[list[ChunkDraft], str | None]:
    """按条款编号分组；同一条款跨页时保持 provision_id 连续。

    carry 是上一页最后一个条款编号：页首续写内容属于跨页条款，
    若不接上会让这些 chunk 的 provision_id 为空（AC-3 要求非空且与原文对应）。
    注意只延续编号，不合并 chunk——chunk 仍严格不跨页。
    """
    out: list[ChunkDraft] = []
    current: str | None = carry
    group: list[PageTextBlock] = []
    pending_heading = False
    page = blocks[0].page_no if blocks else 0

    def flush() -> None:
        nonlocal group
        if group:
            out.extend(_emit(page, group, "provision", current, lang, target, hard_max))
            group = []

    for b in blocks:
        if b.page_no != page:
            flush()
            page = b.page_no
        if BACK_MATTER_RE.match(b.text):
            # 先冲掉上一条款的正文，再把条款号清空：后置正文不属于任何条款，
            # 且不能跨页延续（carry 返回 None，下一页同样从空开始）
            flush()
            current = None
            pending_heading = False
            group.append(b)
            continue
        m = PROVISION_RE.match(b.text)
        if m:
            if pending_heading:
                # 章/节标题块归到它下面第一条条款，避免被错标成上一章的最后一条
                current = m.group(1)
                pending_heading = False
            flush()
            current = m.group(1)
        elif HEADING_RE.match(b.text) and (
                not group or all(HEADING_RE.match(g.text) for g in group)):
            pending_heading = True
        group.append(b)
    flush()
    return out, current


def _chunk_section(blocks: list[PageTextBlock], lang: str,
                   target: int, hard_max: int) -> list[ChunkDraft]:
    """按章节标题分组。"""
    out: list[ChunkDraft] = []
    group: list[PageTextBlock] = []
    page = blocks[0].page_no if blocks else 0

    def flush() -> None:
        nonlocal group
        if group:
            out.extend(_emit(page, group, "section", None, lang, target, hard_max))
            group = []

    for b in blocks:
        if b.page_no != page:
            flush()
            page = b.page_no
        if SECTION_RE.match(b.text) and group:
            flush()
        group.append(b)
    flush()
    return out


def _chunk_paragraph(blocks: list[PageTextBlock], lang: str,
                     target: int, hard_max: int) -> list[ChunkDraft]:
    """段落切分：以块为段落，按目标长度合并相邻段，无重叠。"""
    out: list[ChunkDraft] = []
    page = blocks[0].page_no if blocks else 0
    page_blocks = []
    for b in blocks:
        if b.page_no != page:
            out.extend(_emit(page, page_blocks, "paragraph", None, lang, target, hard_max))
            page_blocks = []
            page = b.page_no
        page_blocks.append(b)
    out.extend(_emit(page, page_blocks, "paragraph", None, lang, target, hard_max))
    return out


def _chunk_semantic(blocks: list[PageTextBlock], lang: str,
                    target: int, hard_max: int) -> list[ChunkDraft]:
    """语义切分：按句末标点断句后再装箱。

    一期不引入额外模型（避免再占一份显存），用标点与长度启发式；
    仅当前三种策略在本文件上不可用时才启用。
    """
    merged: list[PageTextBlock] = []
    for b in blocks:
        buf = ""
        start = b.char_start
        for i, ch in enumerate(b.text):
            buf += ch
            if ch in SENTENCE_END and len(buf) >= 40:
                merged.append(PageTextBlock(
                    page_no=b.page_no, text=buf, bbox=b.bbox,
                    char_start=start, char_end=start + len(buf), ncol=b.ncol,
                ))
                start += len(buf)
                buf = ""
        if buf:
            merged.append(PageTextBlock(
                page_no=b.page_no, text=buf, bbox=b.bbox,
                char_start=start, char_end=start + len(buf), ncol=b.ncol,
            ))
    return _emit(blocks[0].page_no if blocks else 0, merged, "semantic",
                 None, lang, target, hard_max)


def chunk_document(
    parsed: ParsedDocument,
    *,
    strategy: str,
    lang: str,
    target_chars: int = 550,
    max_chars: int = 1100,
    overlap: int = 0,
) -> list[ChunkDraft]:
    """按指定策略切分整份文档（按页独立处理，保证偏移可解释）。"""
    validate_chunk_config(strategy, overlap)

    by_page: dict[int, list[PageTextBlock]] = {}
    for b in parsed.blocks:
        by_page.setdefault(b.page_no, []).append(b)

    drafts: list[ChunkDraft] = []
    carry_prov: str | None = None
    for page_no in sorted(by_page):
        blocks = by_page[page_no]
        if strategy == "provision":
            # 跨页续写的 chunk 要接上上一页最后的条款号，否则 provision_id 为空
            part, carry_prov = _chunk_provision(
                blocks, lang, target_chars, max_chars, carry_prov)
        elif strategy == "section":
            part = _chunk_section(blocks, lang, target_chars, max_chars)
        elif strategy == "paragraph":
            part = _chunk_paragraph(blocks, lang, target_chars, max_chars)
        elif strategy == "semantic":
            part = _chunk_semantic(blocks, lang, target_chars, max_chars)
        elif strategy == "table_cell":
            # 表格单元切分需要结构化表格输入，由 OCR/MinerU 提供；纯文本层无表格时为空
            part = []
        else:
            raise ValueError(f"未知切分策略 {strategy}")
        drafts.extend(part)

    # 尾块合并：小于 min 且同页同条款时并入前一 chunk，避免碎片化
    return _merge_small_tail(drafts, min_chars=max(40, target_chars // 8))


def _merge_small_tail(drafts: list[ChunkDraft], *, min_chars: int) -> list[ChunkDraft]:
    """把过短的同页同条款尾块并入前一块（仍不重叠）。"""
    out: list[ChunkDraft] = []
    for d in drafts:
        if (out and len(d.text) < min_chars
                and out[-1].page_no == d.page_no
                and out[-1].provision_id == d.provision_id
                and len(out[-1].text) + len(d.text) <= 1200):
            prev = out[-1]
            # 与 _emit 同一规则：偏移连续说明原文此处没有换行
            sep = "" if d.char_start == prev.char_end else "\n"
            prev.text = prev.text + sep + d.text
            prev.char_end = max(prev.char_end, d.char_end)
            continue
        out.append(d)
    return out


def make_chunk_id(doc_id: str, version_no: int, page_no: int,
                  char_start: int, char_end: int) -> str:
    """chunk_id = sha256(doc_id|version|page|start|end) 前 32 位，天然幂等。"""
    raw = f"{doc_id}|{version_no}|{page_no}|{char_start}|{char_end}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
