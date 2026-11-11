"""结构化通道：白名单 SQL + 硬限行 + 超时 + 只读鉴权（R-24）。

四条约束（PRD 5.5）：
    1. 只读：连接以 `mode=ro` 打开，并用 SQLite authorizer 再拦一层；
    2. 白名单：只允许检索视图与 chunks/documents/pnp_mappings/provisions；
    3. `LIMIT 200`：无论 SQL 是否自带 LIMIT，取数最多 200 行并标 truncated；
    4. 5 秒超时：用 progress_handler 中断长查询。

`calc_results`、`firm_profiles` 等台账表在此被拒，并写审计（AC-22）。
SQLite 没有数据库账号体系，故以「只读连接 + authorizer 白名单」替代
「检索服务账号无台账权限」，该差异登记为 M1 规格偏差（D-10）。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from ulid import ULID

from app.config import Settings
from app.core.db import Database
from app.core.logging import get_logger
from app.errors import AppError
from app.retrieve.types import Candidate

# authorizer 放行的基表：视图展开时 SQLite 会对基表发 SQLITE_READ
ALLOWED_TABLES = {
    "v_provision_index", "v_chunk_meta", "v_document_meta", "v_mappings",
    "chunks", "documents", "pnp_mappings", "provisions",
}

# 台账与敏感表：出现在 SQL 任意位置即拒绝，并留审计
FORBIDDEN_TABLES = {
    "calc_results", "firm_profiles", "users", "role_assignments",
    "findings", "cot_chains", "evidence_refs", "audit_logs", "egress_logs",
    "model_snapshots", "eval_sets", "eval_results", "badcases",
    "gaps_register", "review_tasks",
}

_FORBIDDEN_KEYWORDS = (
    "attach", "detach", "pragma", "insert", "update", "delete", "drop",
    "alter", "create", "replace", "vacuum", "reindex", "with", "union",
)
_TABLE_REF_RE = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
_PROVISION_RE = re.compile(r"(\d{1,2}\.[A-Z]\.\d{1,3})")
_COMMENT_RE = re.compile(r"(--|/\*)")


@dataclass
class SqlResult:
    """一次受控查询的结果。"""

    rows: list[dict] = field(default_factory=list)
    truncated: bool = False
    sql: str = ""


def validate_sql(sql: str) -> tuple[str, list[str]]:
    """校验并归一化 SQL；返回 (SQL, 引用的表名列表)。不合法即抛错。"""
    text = (sql or "").strip()
    if not text:
        raise AppError("SQL_GUARD_REJECTED", "空 SQL 被拒绝")
    # 只允许单条语句：去掉末尾分号后仍有分号即视为多语句
    text = text.rstrip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    if ";" in text:
        raise AppError("SQL_GUARD_REJECTED", "禁止一次提交多条 SQL 语句")
    if _COMMENT_RE.search(text):
        raise AppError("SQL_GUARD_REJECTED", "SQL 中禁止出现注释")
    lowered = text.lower()
    if not lowered.startswith("select"):
        raise AppError("SQL_GUARD_REJECTED", "结构化通道只允许 SELECT")
    for word in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{word}\b", lowered):
            raise AppError("SQL_GUARD_REJECTED", f"禁止的关键字：{word}")

    hit_forbidden = [t for t in FORBIDDEN_TABLES if re.search(rf"\b{t}\b", lowered)]
    if hit_forbidden:
        raise AppError(
            "SQL_GUARD_REJECTED",
            f"拒绝访问非白名单表：{', '.join(sorted(hit_forbidden))}",
            details={"tables": sorted(hit_forbidden)},
        )

    tables = [m.group(1).lower() for m in _TABLE_REF_RE.finditer(text)]
    if not tables:
        raise AppError("SQL_GUARD_REJECTED", "未识别到查询表，拒绝执行")
    for name in tables:
        if name not in ALLOWED_TABLES:
            raise AppError(
                "SQL_GUARD_REJECTED",
                f"表 {name} 不在结构化通道白名单内",
                details={"table": name},
            )
    return text, tables


class StructuredChannel:
    """结构化检索通道。"""

    def __init__(self, db_path: Path, settings: Settings, db: Database) -> None:
        self._db_path = Path(db_path)
        self._settings = settings
        self._audit_db = db
        self._log = get_logger("retrieval")

    # ---------- 连接与鉴权 ----------
    def connect_readonly(self) -> sqlite3.Connection:
        """只读连接 + authorizer 白名单（AC-26 的鉴权替身）。"""
        uri = f"file:{self._db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=self._settings.retrieval_sql_timeout_s)
        conn.row_factory = sqlite3.Row
        conn.set_authorizer(self._authorizer)
        return conn

    @staticmethod
    def _authorizer(action, arg1, arg2, dbname, source):  # noqa: ANN001
        """只放行 SELECT 与白名单表的 READ，其余一律 DENY。"""
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION,
                      getattr(sqlite3, "SQLITE_RECURSIVE", 33)):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            table = (arg1 or "").lower()
            return sqlite3.SQLITE_OK if table in ALLOWED_TABLES else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY

    # ---------- 审计 ----------
    def _write_audit(self, actor: str, action: str, target: str) -> None:
        """追加一条审计；失败只记日志，不阻断拒绝结果。"""
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        log_id = f"aud_{ULID()}"
        try:
            def _write(conn) -> None:
                row = conn.execute(
                    "SELECT self_hash FROM audit_logs ORDER BY created_at DESC, log_id DESC LIMIT 1"
                ).fetchone()
                prev_hash = row["self_hash"] if row else None
                raw = f"{actor}|{action}|{target}|{prev_hash or ''}"
                self_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                conn.execute(
                    "INSERT INTO audit_logs (log_id, actor, action, target, prev_hash, "
                    "self_hash, created_at) VALUES (?,?,?,?,?,?,?)",
                    (log_id, actor, action, target, prev_hash, self_hash, created),
                )
            self._audit_db.run_write(_write)
        except Exception as exc:  # 审计失败不得掩盖原始拒绝语义
            self._log.error("audit_write_failed", action=action, reason=str(exc)[:200])

    # ---------- 受控执行 ----------
    def execute_sql(self, sql: str, *, actor: str = "retrieval",
                    params: tuple = ()) -> SqlResult:
        """校验 + 限行 + 超时执行；任何一步不合规都拒绝。"""
        try:
            text, _tables = validate_sql(sql)
        except AppError as exc:
            # AC-22：越表访问必须留审计，记录被点名的表
            target = ",".join(exc.details.get("tables", [])) or "unknown"
            self._write_audit(actor, "SQL_GUARD_REJECTED", target)
            self._log.warning("sql_guard_rejected", actor=actor, target=target,
                              reason=exc.message[:200])
            raise

        limit = self._settings.retrieval_sql_max_rows
        deadline = time.monotonic() + self._settings.retrieval_sql_timeout_s
        conn = self.connect_readonly()
        try:
            # progress_handler 返回非 0 会中断查询，用它实现 5 秒硬超时
            conn.set_progress_handler(
                lambda: 1 if time.monotonic() > deadline else 0, 10_000
            )
            cur = conn.execute(text, params)
            rows = cur.fetchmany(limit + 1)
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise AppError("UPSTREAM_TIMEOUT", "结构化查询超过 5 秒，已中断") from exc
            raise AppError("SQL_GUARD_REJECTED", f"SQL 执行失败：{exc}") from exc
        except sqlite3.DatabaseError as exc:
            raise AppError("SQL_GUARD_REJECTED", f"SQL 执行被拒绝：{exc}") from exc
        finally:
            conn.close()

        truncated = len(rows) > limit
        return SqlResult(rows=[dict(r) for r in rows[:limit]],
                         truncated=truncated, sql=text)

    # ---------- 通道检索 ----------
    @staticmethod
    def extract_provision_id(query: str) -> str | None:
        """从查询中提取条款编号；没有则不启用结构化通道。"""
        m = _PROVISION_RE.search(query or "")
        return m.group(1) if m else None

    def search(self, query: str, *, index_versions: list[str],
               depts: list[str], clearances: list[str]) -> list[Candidate]:
        """按条款编号做精确结构化检索。

        M1 不引入 text-to-SQL 模型：结构化意图限定为「查询中含条款编号」，
        元数据/映射类自然语言查询留到 M2 再接生成式 SQL。这条边界在报告中说明。
        """
        pid = self.extract_provision_id(query)
        if not pid or not index_versions or not depts or not clearances:
            return []
        placeholders_v = ",".join("?" for _ in index_versions)
        placeholders_d = ",".join("?" for _ in depts)
        placeholders_c = ",".join("?" for _ in clearances)
        sql = (
            "SELECT provision_id, doc_id, version_no, page_start, snippet "
            "FROM v_provision_index "
            f"WHERE provision_id = ? AND index_version IN ({placeholders_v}) "
            f"AND dept IN ({placeholders_d}) AND clearance IN ({placeholders_c}) "
            f"LIMIT {self._settings.retrieval_sql_max_rows}"
        )
        params = (pid, *index_versions, *depts, *clearances)
        result = self.execute_sql(sql, params=params)

        out: list[Candidate] = []
        for row in result.rows:
            doc_id = str(row.get("doc_id") or "")
            chunk_id = "sql_" + hashlib.sha256(
                f"{doc_id}|{row.get('provision_id')}|{row.get('version_no')}".encode()
            ).hexdigest()[:24]
            out.append(Candidate(
                chunk_id=chunk_id,
                provision_id=str(row.get("provision_id") or "") or None,
                score=1.0,
                channel="sql",
                page_no=int(row.get("page_start") or 0),
                text=str(row.get("snippet") or ""),
                doc_id=doc_id,
                lang="",
            ))
        return out
