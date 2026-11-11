"""SQLite 访问层：单写连接 + 写队列 + WAL，满足台账「不可篡改」的前提。

为什么不用 ORM：本项目要求每条 DDL/DML 都能被审计与重放，ORM 生成的隐式语句
会让「谁在什么时候改了哪张表」变得不可回答。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

T = TypeVar("T")

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"
TRIGGERS_PATH = Path(__file__).resolve().parents[2] / "db" / "triggers.sql"
VIEWS_PATH = Path(__file__).resolve().parents[2] / "db" / "views.sql"


class Database:
    """SQLite 门面。

    - 写操作全部经 `write()`，由进程内锁串行化（契约 2.4 并发规则 2）。
    - 读操作使用独立连接，WAL 模式下不阻塞写。
    - 连接级 PRAGMA 每次打开都设置，避免外部工具改动影响本进程。
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        """建立连接并设置 WAL、外键与忙等超时。"""
        conn = sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def init_schema(self) -> None:
        """建表与触发器；重复执行安全。

        executescript 会自行提交事务，因此不能包在 BEGIN IMMEDIATE 里，
        否则脚本执行完毕时事务已结束，外层 COMMIT/ROLLBACK 都会报
        「no transaction is active」。
        """
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        triggers = TRIGGERS_PATH.read_text(encoding="utf-8")
        views = VIEWS_PATH.read_text(encoding="utf-8") if VIEWS_PATH.exists() else ""
        conn = self._connect()
        try:
            self._prepare_cot_chains_migration(conn)
            self._prepare_documents_migration(conn)
            self._prepare_review_tasks_migration(conn)
            conn.executescript(sql)
            self._add_review_tasks_business_code(conn)
            self._finish_cot_chains_migration(conn)
            self._finish_documents_migration(conn)
            self._finish_review_tasks_migration(conn)
            conn.executescript(triggers)
            conn.executescript(views)
        finally:
            conn.close()

    @staticmethod
    def _prepare_cot_chains_migration(conn: sqlite3.Connection) -> None:
        """老库的 cot_chains.finding_id 是 NOT NULL，回验失败时无 finding 可挂。

        M2 起改为 finding_id 可空并新增 task_id；旧表先改名，等新表建好后搬数据。
        """
        cols = [row[1] for row in conn.execute("PRAGMA table_info(cot_chains)")]
        if not cols or "task_id" in cols:
            return
        conn.executescript(
            "DROP TRIGGER IF EXISTS trg_cot_chains_no_update;"
            "DROP TRIGGER IF EXISTS trg_cot_chains_no_delete;"
            "ALTER TABLE cot_chains RENAME TO cot_chains_legacy;"
        )

    @staticmethod
    def _add_review_tasks_business_code(conn: sqlite3.Connection) -> None:
        """DEF-Q0-03：老库的 review_tasks 没有 business_code 列。

        纯加列（无 CHECK 参与），ALTER TABLE 即可；幂等。
        """
        cols = [row[1] for row in conn.execute("PRAGMA table_info(review_tasks)")]
        if not cols or "business_code" in cols:
            return
        conn.execute("ALTER TABLE review_tasks ADD COLUMN business_code TEXT")

    @staticmethod
    def _prepare_review_tasks_migration(conn: sqlite3.Connection) -> None:
        """老库 review_tasks.task_type 只允许 qa/formal，异步入库需要 ingest。

        CHECK 约束不能原地修改，只能重建表。与 documents 迁移同理：先改名，
        等 schema.sql 建出新表后再搬数据；`legacy_alter_table=ON` 保证
        findings/cot_chains 的外键仍指向 review_tasks，而不是跟着改到临时表。
        """
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='review_tasks'"
        ).fetchone()
        if row is None or "'ingest'" in (row["sql"] or ""):
            return
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute("ALTER TABLE review_tasks RENAME TO review_tasks_legacy")
        conn.execute("PRAGMA legacy_alter_table=OFF")

    @staticmethod
    def _finish_review_tasks_migration(conn: sqlite3.Connection) -> None:
        """把旧任务行搬入新表后删除临时表，并恢复外键检查。"""
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_tasks_legacy'"
        ).fetchone()
        if exists is None:
            return
        cols = [row[1] for row in conn.execute(
            "PRAGMA table_info(review_tasks_legacy)")]
        # 更老的库连 business_code 都没有，缺失时按 NULL 搬
        business_code = "business_code" if "business_code" in cols else "NULL"
        conn.executescript(
            "INSERT INTO review_tasks (task_id, task_type, supply, inference_scope, "
            "scope_json, profile_version, business_code, status, queued_at, "
            "started_at, finished_at) "
            f"SELECT task_id, task_type, supply, inference_scope, scope_json, "
            f"profile_version, {business_code}, status, queued_at, started_at, "
            "finished_at FROM review_tasks_legacy;"
            "DROP TABLE review_tasks_legacy;"
        )
        conn.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _finish_cot_chains_migration(conn: sqlite3.Connection) -> None:
        """把旧链数据搬入新表后删除临时表。"""
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cot_chains_legacy'"
        ).fetchone()
        if exists is None:
            return
        conn.executescript(
            "INSERT INTO cot_chains (chain_id, task_id, finding_id, step_no, "
            "step_type, payload_json, anchors_json, validator_result, prev_hash, "
            "self_hash, created_at) "
            "SELECT chain_id, NULL, finding_id, step_no, step_type, payload_json, "
            "anchors_json, validator_result, prev_hash, self_hash, created_at "
            "FROM cot_chains_legacy;"
            "DROP TABLE cot_chains_legacy;"
        )

    @staticmethod
    def _prepare_documents_migration(conn: sqlite3.Connection) -> None:
        """老库的 documents.index_status 没有 display_only（中文译版等显示层）。

        CHECK 约束不能原地修改，只能重建表：先把旧表改名，等 schema.sql 建出
        新表后再搬数据。`legacy_alter_table=ON` 保证 chunks 的外键仍指向
        documents，而不是被跟着改成 documents_legacy。
        """
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()
        if row is None or "display_only" in (row["sql"] or ""):
            return
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute("ALTER TABLE documents RENAME TO documents_legacy")
        conn.execute("PRAGMA legacy_alter_table=OFF")

    @staticmethod
    def _finish_documents_migration(conn: sqlite3.Connection) -> None:
        """把旧文档行搬入新表后删除临时表，并恢复外键检查。"""
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents_legacy'"
        ).fetchone()
        if exists is None:
            return
        conn.executescript(
            "INSERT INTO documents (doc_id, version_no, corpus_type, doc_type, dept, "
            "clearance, effective_from, effective_to, file_hash, file_path, file_size, "
            "page_count, status, parser, text_layer_ratio, index_status, index_version, "
            "active_version, deleted_at, created_at) "
            "SELECT doc_id, version_no, corpus_type, doc_type, dept, clearance, "
            "effective_from, effective_to, file_hash, file_path, file_size, page_count, "
            "status, parser, text_layer_ratio, index_status, index_version, "
            "active_version, deleted_at, created_at FROM documents_legacy;"
            "DROP TABLE documents_legacy;"
        )
        conn.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """只读连接，使用完自动关闭。"""
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """写连接：一把进程内锁 + 显式事务，异常即整体回滚。"""
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """只读查询，返回全部行。"""
        with self.connect() as conn:
            return list(conn.execute(sql, params).fetchall())

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        """只读查询，返回首行或 None。"""
        with self.connect() as conn:
            return conn.execute(sql, params).fetchone()

    def run_write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """把一段多语句逻辑放进同一个写事务里执行。"""
        with self.write() as conn:
            return fn(conn)


_db_cache: dict[str, Database] = {}
_db_cache_lock = threading.Lock()


def get_database(db_path: Path) -> Database:
    """按路径缓存 Database 实例，避免每次请求重建锁。"""
    key = str(Path(db_path).resolve())
    with _db_cache_lock:
        if key not in _db_cache:
            _db_cache[key] = Database(Path(db_path))
        return _db_cache[key]
