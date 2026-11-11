"""AC-6：解析分派 —— 有文本层的 PDF 不产生任何 OCR 调用。"""
from __future__ import annotations

import fitz
import pytest

from app.parse.ocr.base import OcrBlock, OcrResult
from app.parse.router import DocumentRouter


class CountingGateway:
    """记录调用次数的出域网关替身。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def ocr_page(self, **kwargs) -> OcrResult:
        self.calls.append(kwargs)
        image = kwargs["image"]
        return OcrResult(
            blocks=[OcrBlock(text="扫描页识别文本", bbox=(0, 0, 10, 10), confidence=0.9)],
            page_width=image.width_px,
            page_height=image.height_px,
            provider="fake",
            provider_request_id="req-1",
            confidence_avg=0.9,
        )


def _text_pdf(path) -> None:
    """生成两页、每页字符数远超 200 的原生 PDF。"""
    doc = fitz.open()
    for _ in range(2):
        page = doc.new_page()
        line = "The firm must document its policies and procedures in writing. " * 5
        page.insert_text((72, 72), line[:400], fontsize=10)
        page.insert_text((72, 96), line[:400], fontsize=10)
    doc.save(path)
    doc.close()


def _scanned_pdf(path) -> None:
    """生成只有图形的「扫描件」：无任何文本层。"""
    doc = fitz.open()
    page = doc.new_page()
    page.draw_rect(fitz.Rect(50, 50, 500, 700), color=(0, 0, 0), fill=(0.4, 0.4, 0.4))
    doc.save(path)
    doc.close()


def test_text_pdf_never_calls_ocr(settings, tmp_path):
    """原生 PDF 走文本解析，OCR 调用次数必须为 0。"""
    pdf = tmp_path / "text.pdf"
    _text_pdf(pdf)
    gateway = CountingGateway()
    outcome = DocumentRouter(settings, gateway).parse(
        str(pdf), doc_id="DOC-T", version_no=1, corpus_type="A_spec")
    assert outcome.parser == "text"
    assert outcome.used_ocr is False
    assert gateway.calls == []
    assert outcome.parsed.blocks


def test_scanned_pdf_goes_ocr(settings, tmp_path):
    """无文本层走 OCR，且出域前已裁边。"""
    pdf = tmp_path / "scan.pdf"
    _scanned_pdf(pdf)
    gateway = CountingGateway()
    outcome = DocumentRouter(settings, gateway).parse(
        str(pdf), doc_id="DOC-S", version_no=1, corpus_type="B_internal",
        ocr_approval_ref="APR-1")
    assert outcome.parser == "ocr"
    assert outcome.used_ocr is True
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["image"].box_px["x0"] == settings.ocr_crop_margin_x


def test_ocr_failure_marks_document_failed(settings, tmp_path):
    """OCR 全失败时必须标 ocr_failed，而不是返回一份缺页结果。"""
    class FailingGateway:
        def ocr_page(self, **kwargs):
            from app.errors import AppError
            raise AppError("UPSTREAM_TIMEOUT", "两家供应商均失败")

    pdf = tmp_path / "scan2.pdf"
    _scanned_pdf(pdf)
    outcome = DocumentRouter(settings, FailingGateway()).parse(
        str(pdf), doc_id="DOC-F", version_no=1, corpus_type="B_internal",
        ocr_approval_ref="APR-1")
    assert outcome.ok is False
    assert outcome.failures and outcome.failures[0].code == "UPSTREAM_TIMEOUT"
