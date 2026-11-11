"""M0 端到端：上传排队 -> 解析 -> 清洗 -> 切分 -> 两阶段索引提交。

上传接口返回 202 + task_id（AC-75「写请求进入排队」），处理结果从
`GET /reviews/{task_id}` 读取；任务失败时 business_code 记原因。
"""
from __future__ import annotations

import time

import fitz
from fastapi.testclient import TestClient

from app.main import build_context, create_app


class FakeMilvus:
    """内存向量库替身，避免端到端测试依赖 Docker 中的 Milvus。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def insert(self, rows: list[dict]) -> int:
        for row in rows:
            self.rows[row["chunk_id"]] = row
        return len(rows)

    def count(self, index_version: str) -> int:
        return sum(1 for r in self.rows.values() if r["index_version"] == index_version)

    def fetch_texts(self, chunk_ids: list[str]) -> dict[str, str]:
        return {c: self.rows[c]["text"] for c in chunk_ids if c in self.rows}


def _text_pdf_bytes() -> bytes:
    """生成一份两页、带条款编号的原生 PDF。"""
    doc = fitz.open()
    for i in (1, 2):
        page = doc.new_page()
        body = (f"1.A.{i} The firm must document policies and procedures in writing "
                "and retain records for a minimum of five years. ") * 3
        page.insert_text((72, 72), body[:420], fontsize=9)
        page.insert_text((72, 96), body[:420], fontsize=9)
    raw = doc.tobytes()
    doc.close()
    return raw


def _scanned_pdf_bytes() -> bytes:
    """生成一份无文本层的 PDF：必须走云 OCR，用来验证关闭通道后的失败语义。"""
    doc = fitz.open()
    doc.new_page()
    raw = doc.tobytes()
    doc.close()
    return raw


def _wait_task(client: TestClient, task_id: str, timeout: float = 20.0) -> dict:
    """轮询任务到终态；超时即失败，避免测试掩盖队列卡死。"""
    deadline = time.time() + timeout
    body: dict = {}
    while time.time() < deadline:
        body = client.get(f"/reviews/{task_id}").json()
        if body["task"]["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 在 {timeout}s 内未到终态：{body}")


def test_healthz(settings):
    """服务能启动且健康检查返回 ok。"""
    app = create_app(build_context(settings, writer=FakeMilvus()))
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_upload_activates_index(settings):
    """端到端：上传返回 202，队列处理后文档 published，chunk 全部 active。"""
    writer = FakeMilvus()
    ctx = build_context(settings, writer=writer)
    app = create_app(ctx)
    with TestClient(app) as client:
        resp = client.post(
            "/documents",
            files={"file": ("manual.pdf", _text_pdf_bytes(), "application/pdf")},
            data={
                "doc_id": "GIPS-2020", "version_no": "1", "corpus_type": "A_spec",
                "doc_type": "standard", "dept": "FIRM", "clearance": "internal",
                "effective_from": "2020-01-01T00:00:00Z",
            },
        )
        assert resp.status_code == 202, resp.text
        accepted = resp.json()
        assert accepted["status"] == "uploaded"
        assert accepted["duplicate"] is False
        assert accepted["queue_position"] == 0
        final = _wait_task(client, accepted["task_id"])

    assert final["task"]["status"] == "succeeded"
    assert final["task"]["task_type"] == "ingest"
    assert final["document"]["index_status"] == "active"
    assert final["document"]["active_version"] == 1
    assert final["document"]["status"] == "published"
    assert final["document"]["parser"] == "text"

    row = ctx.db.fetch_one(
        "SELECT status, index_status, active_version FROM documents WHERE doc_id='GIPS-2020'"
    )
    assert row["index_status"] == "active"
    assert row["active_version"] == 1
    assert row["status"] == "published"

    active = ctx.db.fetch_one(
        "SELECT COUNT(*) AS c FROM chunks WHERE doc_id='GIPS-2020' AND status='active'"
    )
    assert active["c"] > 0
    assert writer.count(final["document"]["index_version"]) == active["c"]


def test_upload_rejects_c_ledger(settings):
    """C_ledger 在上传接口即被拒，库里不会留下 documents 行。"""
    ctx = build_context(settings, writer=FakeMilvus())
    app = create_app(ctx)
    with TestClient(app) as client:
        resp = client.post(
            "/documents",
            files={"file": ("ledger.pdf", _text_pdf_bytes(), "application/pdf")},
            data={
                "doc_id": "LEDGER-1", "version_no": "1", "corpus_type": "C_ledger",
                "doc_type": "ledger", "dept": "FUND", "clearance": "restricted",
                "effective_from": "2026-01-01T00:00:00Z",
            },
        )
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION_FAILED"
    assert ctx.db.fetch_one("SELECT * FROM documents WHERE doc_id='LEDGER-1'") is None


def test_upload_queues_then_fails_when_egress_disabled(settings):
    """AC-75：云通道关闭时写请求仍被受理（202 排队），由队列落 failed + ocr_failed。"""
    ctx = build_context(settings.model_copy(update={"egress_enabled": False}),
                        writer=FakeMilvus())
    app = create_app(ctx)
    with TestClient(app) as client:
        resp = client.post(
            "/documents",
            files={"file": ("scan.pdf", _scanned_pdf_bytes(), "application/pdf")},
            data={
                "doc_id": "SCAN-1", "version_no": "1", "corpus_type": "A_spec",
                "doc_type": "standard", "dept": "FIRM", "clearance": "internal",
                "effective_from": "2026-01-01T00:00:00Z",
            },
        )
        assert resp.status_code == 202, resp.text
        final = _wait_task(client, resp.json()["task_id"])

    assert final["task"]["status"] == "failed"
    assert final["task"]["business_code"] == "ocr_failed"
    assert final["document"]["status"] == "failed"

    log = ctx.db.fetch_one(
        "SELECT dlp_verdict, state FROM egress_logs ORDER BY rowid DESC LIMIT 1")
    assert log["dlp_verdict"] == "egress_disabled"
    assert log["state"] == "failed"
