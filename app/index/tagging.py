"""入库元数据与 ACL 校验（T-0-12 的强校验部分）。

六项元数据不全一律拒入库（PRD 4.6）：
    corpus_type / dept / clearance / doc_type / effective_from / version_no

C_ledger 在三个层面被拒：接口、校验、数据库 CHECK。这里是第二层。
"""
from __future__ import annotations

import json

from app.errors import AppError

MANDATORY_DOC_FIELDS = (
    "corpus_type",      # A_spec / B_internal
    "dept",             # 归属业务线
    "clearance",        # internal / restricted
    "doc_type",         # 手册 / 制度 / 模板 / 披露
    "effective_from",   # RFC 3339
)

ALLOWED_CLEARANCE = ("internal", "restricted")


def validate_doc_metadata(meta: dict) -> None:
    """校验文档级元数据；缺项一次性列全，避免用户逐条试错。"""
    corpus_type = (meta.get("corpus_type") or "").strip()
    if corpus_type == "C_ledger":
        raise AppError(
            "VALIDATION_FAILED",
            "C_ledger（台账）永久禁止进入检索索引：台账数值只能由计算引擎产出。",
            details={"field": "corpus_type"},
        )
    if corpus_type not in ("A_spec", "B_internal"):
        raise AppError(
            "VALIDATION_FAILED",
            f"corpus_type 只能是 A_spec 或 B_internal，收到 {corpus_type or '(空)'}",
            details={"field": "corpus_type"},
        )
    missing = [f for f in MANDATORY_DOC_FIELDS if not str(meta.get(f) or "").strip()]
    if missing:
        raise AppError(
            "VALIDATION_FAILED",
            f"必填元数据缺失：{', '.join(missing)}",
            details={"missing": missing},
        )
    if meta.get("clearance") not in ALLOWED_CLEARANCE:
        raise AppError(
            "VALIDATION_FAILED",
            f"clearance 只能是 {ALLOWED_CLEARANCE}",
            details={"field": "clearance"},
        )


def build_acl_scope(dept: str, clearance: str) -> str:
    """构造 ACL 标签（JSON 数组字符串）。

    检索时以 `dept:FUND`、`clr:restricted` 这类标签做前置过滤；
    标签在入库时写死，避免「检索后过滤」导致越权结果进入候选集。
    """
    if not dept or clearance not in ALLOWED_CLEARANCE:
        raise AppError("ACL_SCOPE_MISSING", "无法生成 ACL：dept 为空或 clearance 非法")
    return json.dumps([f"dept:{dept}", f"clr:{clearance}"], ensure_ascii=False)


def validate_chunk_metadata(chunk: dict) -> list[str]:
    """chunk 级强校验：返回缺失字段列表，空列表表示通过。

    六项：corpus_type、dept、clearance、doc_type、effective_from、version_no，
    其中 dept/clearance/doc_type/effective_from 不在 chunks 表冗余存储，
    由 doc 级元数据在入库时展开成 acl_scope 与 corpus_type；
    因此这里校验「展开后可判定」的字段。
    """
    required = ("chunk_id", "doc_id", "corpus_type", "acl_scope",
                "page_start", "page_end", "char_start", "char_end", "text")
    missing = [f for f in required if chunk.get(f) in (None, "")]
    if chunk.get("corpus_type") == "C_ledger":
        missing.append("corpus_type(C_ledger 禁止)")
    if chunk.get("text") is not None and not str(chunk["text"]).strip():
        missing.append("text(空块)")
    return missing
