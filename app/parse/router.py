"""解析分派（T-0-09）。

判据只有一个：文本层字符数，不看文件后缀，也不看扩展名。
    - 每页 ≥ 200 字符占比 ≥ 80%  -> 走文本解析，**不产生任何 OCR 调用**
    - 其余（扫描件/低文本层）    -> 逐页裁边后送云 OCR
    - 复杂版式（多栏 + 表格密集） -> M0 只记录告警，MinerU 接入在后续里程碑

失败语义：任一页 OCR 失败，整份解析结果标 ocr_failed 并进人工复核队列，
不允许把失败页静默跳过——那会让「谁知道这页没解析」变成无人回答的问题。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings
from app.core.egress import EgressGateway
from app.core.logging import get_logger
from app.errors import AppError
from app.parse.ocr.base import OcrResult
from app.parse.ocr.crop import crop_for_egress, render_page
from app.parse.pdf_text import PageTextBlock, ParsedDocument, detect_text_layer, extract


@dataclass
class PageFailure:
    """单页解析失败记录。"""

    page_no: int
    code: str
    message: str


@dataclass
class ParseOutcome:
    """解析结果与可观测字段。"""

    parser: str                                   # text / ocr
    ok: bool
    parsed: ParsedDocument
    used_ocr: bool
    failures: list[PageFailure] = field(default_factory=list)
    # 实际成功送去云 OCR 的页码：验收要用它证明「扫描件确实出域过」，
    # 不能拿 failures 代替（成功时 failures 为空，会得出反结论）
    ocr_pages: list[int] = field(default_factory=list)
    page_text_paths: list[str] = field(default_factory=list)


def _blocks_from_ocr(result: OcrResult, page_no: int) -> tuple[str, list[PageTextBlock]]:
    """把 OCR 结果转成页文本流 + 带码点偏移的块。"""
    parts: list[str] = []
    blocks: list[PageTextBlock] = []
    cursor = 0
    for b in result.blocks:
        text = b.text.strip()
        if not text:
            continue
        if parts:
            cursor += 1  # 换行符也占一个码点
        blocks.append(PageTextBlock(
            page_no=page_no,
            text=text,
            bbox=b.bbox,
            char_start=cursor,
            char_end=cursor + len(text),
            ncol=1,
        ))
        parts.append(text)
        cursor += len(text)
    return "\n".join(parts), blocks


class DocumentRouter:
    """根据文本层覆盖率选择解析路径。"""

    def __init__(self, settings: Settings, gateway: EgressGateway) -> None:
        self._settings = settings
        self._gateway = gateway
        self._log = get_logger("ingest")

    def parse(self, pdf_path: str, *, doc_id: str, version_no: int,
              corpus_type: str, ocr_approval_ref: str | None = None) -> ParseOutcome:
        """解析一份 PDF。"""
        s = self._settings
        report = detect_text_layer(
            pdf_path,
            min_chars=s.text_layer_min_chars_page,
            scan_max_chars=s.text_layer_scan_max_chars_page,
        )

        if report.has_text_layer:
            parsed = extract(pdf_path,
                             header_margin_pt=s.header_margin_pt,
                             footer_margin_pt=s.footer_margin_pt)
            self._log.info("document_parsed", doc_id=doc_id, version_no=version_no,
                           parser="text", used_ocr=False,
                           text_layer_ratio=report.coverage,
                           pages=report.page_count, blocks=len(parsed.blocks))
            return ParseOutcome(parser="text", ok=True, parsed=parsed, used_ocr=False)

        # 低文本层：逐页 OCR。有文本层的页仍走文本抽取，避免无谓出域与计费
        parsed = extract(pdf_path,
                         header_margin_pt=s.header_margin_pt,
                         footer_margin_pt=s.footer_margin_pt)
        text_pages = {b.page_no for b in parsed.blocks}
        pages_text: list[str] = list(parsed.pages_text)
        blocks: list[PageTextBlock] = list(parsed.blocks)
        failures: list[PageFailure] = []
        ocr_pages: list[int] = []

        for page_no in range(1, report.page_count + 1):
            if page_no in text_pages:
                continue
            try:
                page = render_page(pdf_path, page_no, dpi=s.ocr_render_dpi)
                image = crop_for_egress(page, s.ocr_crop_margin_x, s.ocr_crop_margin_y)
                result = self._gateway.ocr_page(
                    doc_id=doc_id,
                    version_no=version_no,
                    page_no=page_no,
                    image=image,
                    corpus_type=corpus_type,
                    approval_ref=ocr_approval_ref,
                )
            except AppError as exc:
                # 审批缺失、C 类禁止等硬拒绝要保留原错误码，便于区分配置问题与网络问题
                failures.append(PageFailure(page_no, exc.code, exc.message))
                self._log.error("ocr_page_failed", doc_id=doc_id, page_no=page_no,
                                code=exc.code)
                continue
            except ValueError as exc:
                failures.append(PageFailure(page_no, "VALIDATION_FAILED", str(exc)))
                self._log.error("ocr_page_invalid", doc_id=doc_id, page_no=page_no,
                                reason=str(exc))
                continue

            page_text, page_blocks = _blocks_from_ocr(result, page_no)
            pages_text[page_no - 1] = page_text
            blocks.extend(page_blocks)
            ocr_pages.append(page_no)

        blocks.sort(key=lambda b: (b.page_no, b.char_start))
        merged = ParsedDocument(pages_text=pages_text, blocks=blocks, report=report)
        ok = not failures
        self._log.info(
            "document_parsed",
            doc_id=doc_id, version_no=version_no, parser="ocr", used_ocr=True,
            text_layer_ratio=report.coverage, pages=report.page_count,
            failed_pages=[f.page_no for f in failures], ok=ok,
        )
        return ParseOutcome(parser="ocr", ok=ok, parsed=merged, used_ocr=True,
                            failures=failures, ocr_pages=ocr_pages)


def persist_page_texts(object_store: Path, doc_id: str, version_no: int,
                       page_texts: list[str]) -> list[str]:
    """把每页规范文本落盘；回验器按 page_no 读它做码点切片，避免重复解析。"""
    target = Path(object_store) / doc_id / f"v{version_no}" / "pages"
    target.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for i, text in enumerate(page_texts, start=1):
        path = target / f"{i}.txt"
        path.write_text(text, encoding="utf-8")
        paths.append(str(path))
    meta = target / "index.json"
    meta.write_text(json.dumps({"doc_id": doc_id, "version_no": version_no,
                                "pages": len(page_texts)}, ensure_ascii=False),
                    encoding="utf-8")
    return paths
