"""AC-12C/D/E：出域三条禁令 —— 无审批不出、C 类不出、失败不静默。"""
from __future__ import annotations

import pytest

from app.core.egress import EgressGateway
from app.errors import AppError
from app.parse.ocr.base import FakeOcrProvider, OcrBlock, OcrResult, ProviderError
from app.parse.ocr.crop import CroppedImage


def _image() -> CroppedImage:
    """构造一张已裁边的页面图（不需要真实 PNG 内容）。"""
    return CroppedImage(png_bytes=b"fake-png", box_px={"x0": 40, "y0": 40, "x1": 800, "y1": 1000},
                        box_pt={"x0": 14.4, "y0": 14.4, "x1": 288.0, "y1": 360.0},
                        width_px=760, height_px=960)


class FailingProvider:
    """模拟供应商不可用。"""

    def __init__(self, name: str) -> None:
        self.name = name

    def recognize(self, image_bytes: bytes, *, timeout_s: int = 30) -> OcrResult:
        raise ProviderError(self.name, "network_down")


def _count(db, where: str = "1=1") -> int:
    return db.fetch_one(f"SELECT COUNT(*) AS c FROM egress_logs WHERE {where}")["c"]


def test_c_ledger_has_no_egress_record(settings, db):
    """AC-12E：C_ledger 直接拒绝，且不产生任何出域记录。"""
    gateway = EgressGateway(settings, db, providers={"aliyun": FakeOcrProvider()})
    with pytest.raises(AppError) as exc:
        gateway.ocr_page(doc_id="D1", version_no=1, page_no=1, image=_image(),
                         corpus_type="C_ledger", approval_ref="APR-1")
    assert exc.value.code == "OCR_FORBIDDEN_CORPUS"
    assert _count(db) == 0


def test_b_without_approval_has_no_sent_record(settings, db):
    """AC-12D：B 类无审批必须拒，且 egress_logs 不得出现 sent。"""
    gateway = EgressGateway(settings, db, providers={"aliyun": FakeOcrProvider()})
    with pytest.raises(AppError) as exc:
        gateway.ocr_page(doc_id="D2", version_no=1, page_no=1, image=_image(),
                         corpus_type="B_internal", approval_ref=None)
    assert exc.value.code == "OCR_APPROVAL_MISSING"
    assert _count(db, "state='sent'") == 0
    assert _count(db, "state='blocked'") == 1


def test_all_providers_failed_is_not_silent(settings, db):
    """AC-12C：两家都失败时抛 UPSTREAM_TIMEOUT，并留 failed 记录。"""
    gateway = EgressGateway(settings, db, providers={
        "aliyun": FailingProvider("aliyun"),
        "tencent": FailingProvider("tencent"),
    })
    with pytest.raises(AppError) as exc:
        gateway.ocr_page(doc_id="D3", version_no=1, page_no=2, image=_image(),
                         corpus_type="B_internal", approval_ref="APR-2")
    assert exc.value.code == "UPSTREAM_TIMEOUT"
    assert _count(db, "state='failed'") == 2
    assert _count(db, "state='sent'") == 0


def test_second_provider_takes_over(settings, db):
    """第一家失败后换第二家，成功则只记一条 sent。"""
    gateway = EgressGateway(settings, db, providers={
        "aliyun": FailingProvider("aliyun"),
        "tencent": FakeOcrProvider("tencent"),
    })
    result = gateway.ocr_page(doc_id="D4", version_no=1, page_no=3, image=_image(),
                              corpus_type="B_internal", approval_ref="APR-3")
    assert result.provider == "tencent"
    assert _count(db, "state='sent'") == 1


def test_idempotency_avoids_double_egress(settings, db):
    """同一幂等键重复调用不产生第二条 sent，避免重复计费。"""
    gateway = EgressGateway(settings, db, providers={"aliyun": FakeOcrProvider()})
    for _ in range(2):
        gateway.ocr_page(doc_id="D5", version_no=1, page_no=1, image=_image(),
                         corpus_type="A_spec", approval_ref=None,
                         idempotency_key="IDEM-1")
    assert _count(db, "state='sent'") == 1
