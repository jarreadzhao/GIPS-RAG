"""AC-11：C_ledger 在接口层与数据库层都被拒绝。"""
from __future__ import annotations

import sqlite3

import pytest

from app.errors import AppError
from app.index.tagging import build_acl_scope, validate_doc_metadata

BASE = {
    "doc_type": "manual",
    "dept": "FUND",
    "clearance": "internal",
    "effective_from": "2026-01-01T00:00:00Z",
}


def test_c_ledger_rejected_at_validation():
    """接口层：C_ledger 永久禁止。"""
    with pytest.raises(AppError) as exc:
        validate_doc_metadata({**BASE, "corpus_type": "C_ledger"})
    assert exc.value.code == "VALIDATION_FAILED"
    assert "C_ledger" in exc.value.message


def test_missing_metadata_lists_all_fields():
    """必填项缺失时一次列全。"""
    with pytest.raises(AppError) as exc:
        validate_doc_metadata({"corpus_type": "A_spec"})
    assert set(exc.value.details["missing"]) >= {"dept", "clearance", "doc_type", "effective_from"}


def test_acl_scope_expands():
    """ACL 标签同时包含业务线与密级。"""
    scope = build_acl_scope("FUND", "restricted")
    assert "dept:FUND" in scope and "clr:restricted" in scope


def test_db_check_rejects_c_ledger(db):
    """数据库层：CHECK 约束同样拒绝，防止绕过接口写入。"""
    with pytest.raises(sqlite3.IntegrityError):
        db.run_write(lambda conn: conn.execute(
            """INSERT INTO documents
               (doc_id, version_no, corpus_type, doc_type, dept, clearance,
                effective_from, file_hash, file_path, file_size, created_at)
               VALUES ('D1',1,'C_ledger','ledger','FUND','internal',
                       '2026-01-01T00:00:00Z','h','p',1,'2026-01-01T00:00:00Z')"""
        ))
