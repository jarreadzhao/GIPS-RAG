"""AC-9/AC-10：两阶段提交 —— 条数不一致或抽样不一致都不得进入 active。"""
from __future__ import annotations

import pytest

from app.core.embed import HashingEmbedder
from app.errors import AppError
from app.index.builder import ChunkRecord, IndexBuilder

NOW = "2026-01-01T00:00:00Z"


class FakeMilvus:
    """内存向量库，可注入条数偏差与文本损坏。"""

    def __init__(self, *, force_count: int | None = None, corrupt_text: bool = False) -> None:
        self.rows: dict[str, dict] = {}
        self.force_count = force_count
        self.corrupt_text = corrupt_text

    def insert(self, rows: list[dict]) -> int:
        for row in rows:
            self.rows[row["chunk_id"]] = row
        return len(rows)

    def count(self, index_version: str) -> int:
        if self.force_count is not None:
            return self.force_count
        return sum(1 for r in self.rows.values() if r["index_version"] == index_version)

    def fetch_texts(self, chunk_ids: list[str]) -> dict[str, str]:
        out = {}
        for cid in chunk_ids:
            row = self.rows.get(cid)
            if row:
                out[cid] = row["text"] + ("-corrupted" if self.corrupt_text else "")
        return out


def _seed_doc(db, doc_id: str = "DOC-1") -> None:
    db.run_write(lambda conn: conn.execute(
        """INSERT INTO documents
           (doc_id, version_no, corpus_type, doc_type, dept, clearance,
            effective_from, file_hash, file_path, file_size, created_at)
           VALUES (?,1,'A_spec','manual','FUND','internal',?,'hash1','p',10,?)""",
        (doc_id, NOW, NOW),
    ))


def _chunks(doc_id: str = "DOC-1", n: int = 3) -> list[ChunkRecord]:
    return [
        ChunkRecord(
            chunk_id=f"c{i:032d}", doc_id=doc_id, version_no=1,
            provision_id="1.A.4", chunk_type="provision", text=f"clause text {i}",
            token_len=4, char_start=i * 10, char_end=i * 10 + 8, page_start=1,
            page_end=1, acl_scope='["dept:FUND"]', corpus_type="A_spec",
            pair_id=None, lang="en", index_version="pending",
            cleaner_version="cleaner-v1",
        )
        for i in range(n)
    ]


def _builder(db, settings, milvus) -> IndexBuilder:
    return IndexBuilder(db, HashingEmbedder(dim=8), milvus, settings)


def _doc_row(db):
    return db.fetch_one("SELECT * FROM documents WHERE doc_id='DOC-1' AND version_no=1")


def test_success_activates_in_one_step(db, settings):
    """正常构建：chunk 全部 active，文档 active_version 指向该版本。"""
    _seed_doc(db)
    milvus = FakeMilvus()
    result = _builder(db, settings, milvus).build(
        _chunks(), doc_id="DOC-1", version_no=1, file_hash="hash1")
    assert result.state == "active"
    assert result.chunk_count == result.milvus_count == 3
    row = _doc_row(db)
    assert row["index_status"] == "active"
    assert row["active_version"] == 1
    assert row["status"] == "published"
    statuses = db.fetch_all("SELECT status FROM chunks WHERE doc_id='DOC-1'")
    assert all(r["status"] == "active" for r in statuses)


def test_count_mismatch_stops_before_active(db, settings):
    """AC-10：条数不一致 -> failed，且不得切换 active。"""
    _seed_doc(db)
    milvus = FakeMilvus(force_count=2)
    with pytest.raises(AppError) as exc:
        _builder(db, settings, milvus).build(
            _chunks(), doc_id="DOC-1", version_no=1, file_hash="hash1")
    assert exc.value.code == "INDEX_NOT_ACTIVE"
    row = _doc_row(db)
    assert row["index_status"] == "failed"
    assert row["active_version"] is None
    statuses = db.fetch_all("SELECT status FROM chunks WHERE doc_id='DOC-1'")
    assert all(r["status"] == "failed" for r in statuses)


def test_sample_mismatch_stops_before_active(db, settings):
    """抽样回查文本不一致 -> 判失败，不进入 active。"""
    _seed_doc(db)
    milvus = FakeMilvus(corrupt_text=True)
    with pytest.raises(AppError) as exc:
        _builder(db, settings, milvus).build(
            _chunks(), doc_id="DOC-1", version_no=1, file_hash="hash1")
    assert exc.value.code == "SPAN_MISMATCH"
    assert _doc_row(db)["index_status"] == "failed"


def test_recover_incomplete_marks_failed(db, settings):
    """AC-9：崩溃遗留的 embedding 中间态在重启时被标 failed，不会被检索。"""
    _seed_doc(db)
    db.run_write(lambda conn: conn.execute(
        "UPDATE documents SET index_status='embedding' WHERE doc_id='DOC-1'"))
    count = _builder(db, settings, FakeMilvus()).recover_incomplete()
    assert count == 1
    assert _doc_row(db)["index_status"] == "failed"
