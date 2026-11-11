"""PDF 文本层解析（T-0-08，由 src/extract.py 改造）。

改造点：
    1. 增加文本层检测（每页字符数阈值），供路由层决定「走文本解析还是云 OCR」。
    2. 保留页码、bbox 与**页内 Unicode 码点偏移**，为后续 span 精确回验提供定位。
    3. 不做任何文字改写：连字符换行、全半角、空白都不归一，
       因为回验器要求 `span_text` 与原件逐字节一致（CT-3）。
    4. 列序判定沿用原实现的「宽度占比 + 中心 x 双峰」判据。

偏移定义（重要）：
    char_start / char_end 相对「本模块按阅读顺序重建的页内规范字符流」，
    按 Unicode 码点计，左闭右开。同一个 doc_id + version_no + page_no 内唯一。

    不用 PyMuPDF 的 get_text('text') 原始流做基准，原因是它按 y 坐标排序，
    双栏页上左栏文字会被右栏打断；而块顺序是按栏排的，两者不一致时
    chunk 的 [min(char_start), max(char_end)] 会横跨到另一栏。
    规范流由块拼接而成（块间一个换行），偏移随拼接累积，天然自洽。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import fitz  # PyMuPDF

# 判定为噪声的整块（页码、水印、下载声明）；只用于过滤，不改写保留块的文字
NOISE_PATTERNS = [
    re.compile(r"^Provided proper attribution", re.I),
    re.compile(r"^arXiv:\S+"),
    re.compile(r"^\s*\d{1,3}\s*$"),
    re.compile(r"^(Downloaded|Preprint)", re.I),
]


@dataclass
class PageTextBlock:
    """页面中的一个文本块（已带页内码点偏移）。"""

    page_no: int
    text: str
    bbox: tuple[float, float, float, float]
    char_start: int
    char_end: int
    ncol: int = 1


@dataclass
class TextLayerReport:
    """文本层检测结果。"""

    page_count: int
    per_page_chars: list[int]
    text_pages: list[int]          # 达到文本层阈值的页码
    scanned_pages: list[int]       # 低于扫描阈值的页码
    coverage: float                # 文本层页数 / 总页数
    has_text_layer: bool


@dataclass
class ParsedDocument:
    """一份文档的解析结果。"""

    pages_text: list[str]                       # 每页原始字符流，索引 0 对应 page_no=1
    blocks: list[PageTextBlock] = field(default_factory=list)
    report: TextLayerReport | None = None


def detect_text_layer(pdf_path: str, *, min_chars: int = 200,
                      scan_max_chars: int = 50) -> TextLayerReport:
    """检测文本层：每页字符数 ≥ min_chars 记为有文本层，< scan_max_chars 记为扫描页。

    文档级判据取「文本层页占比 ≥ 0.8」，因为实际手册常夹带少量纯图页，
    逐页全满足会把整份原生 PDF 误判成扫描件。
    """
    per_page: list[int] = []
    with fitz.open(pdf_path) as doc:
        for page in doc:
            per_page.append(len(page.get_text("text")))
    total = len(per_page)
    text_pages = [i + 1 for i, n in enumerate(per_page) if n >= min_chars]
    scanned_pages = [i + 1 for i, n in enumerate(per_page) if n < scan_max_chars]
    coverage = (len(text_pages) / total) if total else 0.0
    return TextLayerReport(
        page_count=total,
        per_page_chars=per_page,
        text_pages=text_pages,
        scanned_pages=scanned_pages,
        coverage=round(coverage, 4),
        has_text_layer=coverage >= 0.8,
    )


def is_noise(text: str) -> bool:
    """整块噪声判定：纯页码、水印、下载声明。"""
    return any(p.search(text) for p in NOISE_PATTERNS) or len(text.strip()) < 2


def detect_columns(blocks: list[tuple[float, float, float, float, str]],
                   page_w: float) -> int:
    """按块宽度与中心 x 分布判定 1 栏还是 2 栏（沿用实测判据）。"""
    if not blocks:
        return 1
    xs0 = min(b[0] for b in blocks)
    xs1 = max(b[2] for b in blocks)
    text_w = max(xs1 - xs0, 1)
    wide = sum(1 for b in blocks if (b[2] - b[0]) > 0.62 * text_w)
    if wide / len(blocks) >= 0.5:
        return 1
    mid = (xs0 + xs1) / 2
    left = sum(1 for b in blocks if (b[0] + b[2]) / 2 < mid)
    return 2 if left >= 3 and (len(blocks) - left) >= 3 else 1


def rebuild_page_stream(blocks: list[PageTextBlock], page_count: int) -> list[str]:
    """按块顺序重建每页规范字符流，并把块偏移回写成累积值。

    两个调用点：
        1. extract() 抽完块后；
        2. clean() 删完块后 —— 被删块（页眉、参考文献）必须一并从字符流里去掉，
           否则回验切片会把已删的页眉算进 span，字面看起来是「回验失败」。

    块在页内的先后就是阅读顺序（双栏已按栏排序），所以直接累加即可。
    """
    by_page: dict[int, list[PageTextBlock]] = {}
    for b in blocks:
        by_page.setdefault(b.page_no, []).append(b)

    pages_text: list[str] = []
    for page_no in range(1, page_count + 1):
        parts: list[str] = []
        cursor = 0
        for b in by_page.get(page_no, []):
            b.char_start = cursor
            b.char_end = cursor + len(b.text)
            parts.append(b.text)
            cursor += len(b.text) + 1  # +1 为块间换行
        pages_text.append("\n".join(parts))
    return pages_text


def extract(pdf_path: str, *, header_margin_pt: int = 60,
            footer_margin_pt: int = 48, use_noise_filter: bool = True) -> ParsedDocument:
    """提取页面文本块，按阅读顺序重建页内字符流并写入码点偏移。"""
    blocks: list[PageTextBlock] = []

    with fitz.open(pdf_path) as doc:
        page_count = doc.page_count
        for page_no, page in enumerate(doc, start=1):
            page_h = page.rect.height

            raw_blocks = [b for b in page.get_text("blocks") if b[6] == 0]  # 只取文本块
            kept: list[tuple[float, float, float, float, str]] = []
            for x0, y0, x1, y1, text, _bn, _bt in raw_blocks:
                if y0 < header_margin_pt or y1 > page_h - footer_margin_pt:
                    continue  # 页眉页脚安全带
                core = text.strip()
                if not core:
                    continue
                if use_noise_filter and is_noise(core):
                    continue
                kept.append((x0, y0, x1, y1, text))
            if not kept:
                continue

            ncol = detect_columns(kept, page.rect.width)
            if ncol == 2:
                mid = (min(b[0] for b in kept) + max(b[2] for b in kept)) / 2
                kept.sort(key=lambda b: (0 if (b[0] + b[2]) / 2 < mid else 1, b[1]))
            else:
                kept.sort(key=lambda b: b[1])

            for x0, y0, x1, y1, text in kept:
                blocks.append(PageTextBlock(
                    page_no=page_no,
                    text=text.strip(),
                    bbox=(x0, y0, x1, y1),
                    char_start=0,      # 由 rebuild_page_stream 统一回写
                    char_end=0,
                    ncol=ncol,
                ))

    pages_text = rebuild_page_stream(blocks, page_count)
    report = detect_text_layer(pdf_path)
    return ParsedDocument(pages_text=pages_text, blocks=blocks, report=report)
