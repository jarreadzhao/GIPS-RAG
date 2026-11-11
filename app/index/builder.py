"""索引构建与两阶段提交（T-0-12 后半）。

状态机（CT-4）：
    pending -> embedding -> indexed -> active
                        \\-> failed

为什么必须两阶段：
    若边写 Milvus 边把版本标 active，中途崩溃会留下「一半向量 + 已生效」的
    状态，检索会命中残缺语料且没有任何报错。因此 active 切换只能发生在
    最后一条单事务 UPDATE 之后，之前任何一步失败都停在 failed。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Protocol

from app.config import Settings
from app.core.db import Database
from app.core.embed import Embedder
from app.core.logging import get_logger
from app.errors import AppError

# 显示层语料的占位索引版本：中文译版等只供引用对照，不写向量库。
# 检索侧据此排除，避免 active 语言集合里混入 zh 影响稀疏通道判定。
DISPLAY_ONLY_INDEX = "display_only"


@dataclass
class ChunkRecord:
    """待入库的 chunk。"""

    chunk_id: str
    doc_id: str
    version_no: int
    provision_id: str | None
    chunk_type: str
    text: str
    token_len: int
    char_start: int
    char_end: int
    page_start: int
    page_end: int
    acl_scope: str
    corpus_type: str
    pair_id: str | None
    lang: str
    index_version: str
    cleaner_version: str
    # 集合里 dept / clearance 是必填标量字段（用于检索前置过滤），
    # 缺了 Milvus 会直接拒绝插入，所以必须随 chunk 一起带下去
    dept: str = ""
    clearance: str = ""
    status: str = "pending"


@dataclass
class BuildResult:
    """构建结果，字段与 CT-4 对齐。"""

    state: str                      # pending / embedding / indexed / active / failed
    index_version: str
    chunk_count: int
    milvus_count: int
    active_version: int | None
    error: str | None = None


@dataclass
class QualityReport:
    """解析/切分质量闸门指标。"""

    parse_success_rate: float
    empty_chunk_rate: float
    table_misalign_rate: float
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


class MilvusWriter(Protocol):
    """只暴露构建需要的三个动作，便于单测注入内存实现。"""

    def insert(self, rows: list[dict]) -> int:
        """写入向量行，返回实际写入条数。"""

    def count(self, index_version: str) -> int:
        """返回指定 index_version 的条数。"""

    def fetch_texts(self, chunk_ids: list[str]) -> dict[str, str]:
        """按 chunk_id 取回 text，用于抽样回查。"""


class MilvusClientWriter:
    """基于 pymilvus MilvusClient 的真实写入器。"""

    def __init__(self, settings: Settings) -> None:
        from pymilvus import MilvusClient

        from app.index.milvus_schema import ensure_collection

        self._collection = settings.milvus_collection
        self._client = MilvusClient(uri=settings.milvus_uri)
        ensure_collection(self._client, self._collection, settings.embedding_dim)

    def insert(self, rows: list[dict]) -> int:
        """插入并 flush，确保 count 语义可验证。"""
        if not rows:
            return 0
        result = self._client.insert(collection_name=self._collection, data=rows)
        self._client.flush(self._collection)
        return int(result.get("insert_count", len(rows)))

    def count(self, index_version: str) -> int:
        """按 index_version 计数。"""
        rows = self._client.query(
            collection_name=self._collection,
            filter=f'index_version == "{index_version}"',
            output_fields=["count(*)"],
        )
        if not rows:
            return 0
        return int(list(rows[0].values())[0])

    def fetch_texts(self, chunk_ids: list[str]) -> dict[str, str]:
        """按主键批量取回文本。"""
        if not chunk_ids:
            return {}
        quoted = ", ".join(f'"{c}"' for c in chunk_ids)
        rows = self._client.query(
            collection_name=self._collection,
            filter=f"chunk_id in [{quoted}]",
            output_fields=["chunk_id", "text"],
        )
        return {r["chunk_id"]: r["text"] for r in rows}


class UnavailableMilvusWriter:
    """Milvus 不可用时的占位实现：明确报错，绝不静默跳过索引。"""

    def insert(self, rows: list[dict]) -> int:
        raise AppError("INDEX_NOT_ACTIVE", "Milvus 不可用，索引构建中止")

    def count(self, index_version: str) -> int:
        raise AppError("INDEX_NOT_ACTIVE", "Milvus 不可用，无法核对索引条数")

    def fetch_texts(self, chunk_ids: list[str]) -> dict[str, str]:
        raise AppError("INDEX_NOT_ACTIVE", "Milvus 不可用，无法抽样回查")


def build_milvus_writer(settings: Settings) -> MilvusWriter:
    """尝试连接 Milvus；失败时返回占位实现，让服务仍可启动但索引操作显式失败。"""
    try:
        return MilvusClientWriter(settings)
    except Exception as exc:  # 连接失败可能抛多种异常，统一降级为占位
        get_logger("ingest").warning("milvus_unavailable", reason=str(exc)[:200])
        return UnavailableMilvusWriter()


def evaluate_quality(*, page_count: int, failed_pages: int, chunk_count: int,
                     empty_chunks: int, table_pages: int,
                     table_misaligned: int, settings: Settings) -> QualityReport:
    """解析成功率 / 空块率 / 表格错位率三项闸门。"""
    parse_success = 1 - (failed_pages / page_count) if page_count else 1.0
    empty_rate = empty_chunks / chunk_count if chunk_count else 1.0
    misalign = table_misaligned / table_pages if table_pages else 0.0
    failures: list[str] = []
    if parse_success < settings.quality_min_parse_success:
        failures.append(f"解析成功率 {parse_success:.2%} 低于 {settings.quality_min_parse_success:.0%}")
    if empty_rate > settings.quality_max_empty_chunk_rate:
        failures.append(f"空块率 {empty_rate:.2%} 高于 {settings.quality_max_empty_chunk_rate:.0%}")
    if misalign > settings.quality_max_table_misalign_rate:
        failures.append(f"表格错位率 {misalign:.2%} 高于 {settings.quality_max_table_misalign_rate:.0%}")
    return QualityReport(round(parse_success, 4), round(empty_rate, 4),
                         round(misalign, 4), failures)


class IndexBuilder:
    """两阶段提交构建器。"""

    def __init__(self, db: Database, embedder: Embedder, writer: MilvusWriter,
                 settings: Settings) -> None:
        self._db = db
        self._embedder = embedder
        self._writer = writer
        self._settings = settings
        self._log = get_logger("ingest")

    # ---------- 显示层入库 ----------
    def build_display_only(self, chunks: list[ChunkRecord], *, doc_id: str,
                           version_no: int) -> int:
        """只写 SQLite，不建向量索引。

        中文译版是显示层：它要能按 pair_id 回链到原文页码与字符区间，
        但不参与召回。因此 chunk 置 active（可被引用查询取到），
        index_version 固定为 DISPLAY_ONLY_INDEX，检索侧显式排除。
        """
        if not chunks:
            raise AppError("VALIDATION_FAILED", "切分结果为空，拒绝入库显示层")
        for chunk in chunks:
            chunk.index_version = DISPLAY_ONLY_INDEX

        def _write(conn) -> None:
            conn.execute("DELETE FROM chunks WHERE doc_id=? AND version_no=?",
                         (doc_id, version_no))
            conn.executemany(
                """INSERT INTO chunks
                   (chunk_id, doc_id, version_no, provision_id, chunk_type, text,
                    token_len, char_start, char_end, page_start, page_end, acl_scope,
                    corpus_type, pair_id, lang, index_version, cleaner_version, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(c.chunk_id, c.doc_id, c.version_no, c.provision_id, c.chunk_type,
                  c.text, c.token_len, c.char_start, c.char_end, c.page_start,
                  c.page_end, c.acl_scope, c.corpus_type, c.pair_id, c.lang,
                  DISPLAY_ONLY_INDEX, c.cleaner_version, "active") for c in chunks],
            )
            conn.execute(
                "UPDATE documents SET status='published', "
                "index_status='display_only', index_version=? "
                "WHERE doc_id=? AND version_no=?",
                (DISPLAY_ONLY_INDEX, doc_id, version_no),
            )

        self._db.run_write(_write)
        return len(chunks)

    # ---------- 恢复 ----------
    def recover_incomplete(self) -> int:
        """启动时把中间态构建标 failed，保证崩溃后不会有半成品被检索。"""
        def _recover(conn) -> int:
            cur = conn.execute(
                "UPDATE documents SET index_status='failed' WHERE index_status='embedding'"
            )
            return cur.rowcount

        count = self._db.run_write(_recover)
        if count:
            self._log.warning("index_build_recovered", marked_failed=count)
        return count

    # ---------- 构建 ----------
    def build(self, chunks: list[ChunkRecord], *, doc_id: str, version_no: int,
              file_hash: str) -> BuildResult:
        """执行两阶段提交；任何一步失败都停在 failed。"""
        index_version = self._make_version(doc_id, version_no, file_hash)
        # chunk 的 index_version 必须与本次构建一致，否则写进 Milvus 的会是占位值
        for chunk in chunks:
            chunk.index_version = index_version
        if not chunks:
            self._mark_failed(doc_id, version_no)
            raise AppError("VALIDATION_FAILED", "切分结果为空，拒绝建立空索引")

        # 阶段一：SQLite 落 chunk（pending），同一 index_version 重试时先清旧行
        self._stage_one(chunks, doc_id, version_no, index_version)

        # 阶段二：批量嵌入并写 Milvus
        try:
            inserted = self._embed_and_insert(chunks)
            # 以 Milvus 的实际计数为准，不能只信 insert 的返回值（AC-10）
            milvus_count = self._writer.count(index_version)
        except Exception as exc:
            self._mark_failed(doc_id, version_no)
            self._log.error("index_embed_failed", doc_id=doc_id,
                            version_no=version_no, reason=str(exc)[:200])
            raise

        # 条数核对：不一致即失败，严禁进入 active（AC-10）
        if milvus_count != len(chunks) or inserted != len(chunks):
            self._mark_failed(doc_id, version_no)
            self._log.error("index_count_mismatch", doc_id=doc_id,
                            expected=len(chunks), actual=milvus_count)
            raise AppError(
                "INDEX_NOT_ACTIVE",
                f"索引条数不一致：SQLite {len(chunks)} 条，Milvus {milvus_count} 条，构建判失败。",
                details={"expected": len(chunks), "actual": milvus_count},
            )

        self._set_indexed(doc_id, version_no, index_version)

        # 抽样回查：Milvus 返回的 text 必须与本地逐字一致
        mismatch = self._sample_check(chunks)
        if mismatch:
            self._mark_failed(doc_id, version_no)
            self._log.error("index_sample_mismatch", doc_id=doc_id, mismatched=mismatch)
            raise AppError("SPAN_MISMATCH",
                           f"抽样回查有 {len(mismatch)} 条文本不一致，构建判失败")

        # 切换 active：唯一一条事务，成功即生效
        self._activate(doc_id, version_no, index_version)
        return BuildResult(state="active", index_version=index_version,
                           chunk_count=len(chunks), milvus_count=milvus_count,
                           active_version=version_no)

    def _make_version(self, doc_id: str, version_no: int, file_hash: str) -> str:
        """index_version 绑定文件哈希、清洗/切分版本与切分策略，任何一项变更都会产生新版本。"""
        raw = "|".join([doc_id, str(version_no), file_hash,
                        self._settings.cleaner_version, self._settings.chunker_version,
                        self._settings.chunker_strategy])
        return "idx_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _stage_one(self, chunks: list[ChunkRecord], doc_id: str, version_no: int,
                   index_version: str) -> None:
        """把 chunk 写入 SQLite，文档状态置 embedding。"""
        def _write(conn) -> None:
            conn.execute(
                "DELETE FROM chunks WHERE doc_id=? AND version_no=? AND index_version=?",
                (doc_id, version_no, index_version),
            )
            conn.executemany(
                """INSERT INTO chunks
                   (chunk_id, doc_id, version_no, provision_id, chunk_type, text,
                    token_len, char_start, char_end, page_start, page_end, acl_scope,
                    corpus_type, pair_id, lang, index_version, cleaner_version, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(c.chunk_id, c.doc_id, c.version_no, c.provision_id, c.chunk_type,
                  c.text, c.token_len, c.char_start, c.char_end, c.page_start,
                  c.page_end, c.acl_scope, c.corpus_type, c.pair_id, c.lang,
                  index_version, c.cleaner_version, "pending") for c in chunks],
            )
            conn.execute(
                "UPDATE documents SET index_status='embedding', index_version=? "
                "WHERE doc_id=? AND version_no=?",
                (index_version, doc_id, version_no),
            )

        self._db.run_write(_write)

    def _embed_and_insert(self, chunks: list[ChunkRecord]) -> int:
        """按 8 条一批编码并写入向量库，返回写入总数。"""
        batch_size = 8
        total = 0
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start:start + batch_size]
            emb = self._embedder.encode([c.text for c in batch])
            if len(emb.dense) != len(batch) or len(emb.sparse) != len(batch):
                raise AppError("VALIDATION_FAILED", "嵌入返回条数与输入不一致")
            rows = []
            for chunk, dense, sparse in zip(batch, emb.dense, emb.sparse):
                rows.append({
                    "chunk_id": chunk.chunk_id, "doc_id": chunk.doc_id,
                    "version_no": chunk.version_no,
                    "provision_id": chunk.provision_id or "",
                    "corpus_type": chunk.corpus_type,
                    "dept": chunk.dept, "clearance": chunk.clearance,
                    "chunk_type": chunk.chunk_type, "lang": chunk.lang,
                    "pair_id": chunk.pair_id or "",
                    "index_version": chunk.index_version,
                    "cleaner_version": chunk.cleaner_version,
                    "page_start": chunk.page_start, "page_end": chunk.page_end,
                    "char_start": chunk.char_start, "char_end": chunk.char_end,
                    "acl_scope": chunk.acl_scope, "text": chunk.text,
                    "dense": dense, "sparse": sparse,
                })
            total += self._writer.insert(rows)
        return total

    def _sample_check(self, chunks: list[ChunkRecord]) -> list[str]:
        """抽 20 条比对文本哈希；返回不一致的 chunk_id。"""
        sample = chunks[: self._settings.index_sample_check_size]
        remote = self._writer.fetch_texts([c.chunk_id for c in sample])
        bad: list[str] = []
        for c in sample:
            if remote.get(c.chunk_id) != c.text:
                bad.append(c.chunk_id)
        return bad

    def _set_indexed(self, doc_id: str, version_no: int, index_version: str) -> None:
        """向量齐备后把该版本的 chunk 置 indexed。"""
        def _write(conn) -> None:
            conn.execute(
                "UPDATE chunks SET status='indexed' "
                "WHERE doc_id=? AND version_no=? AND index_version=? AND status='pending'",
                (doc_id, version_no, index_version),
            )
            conn.execute(
                "UPDATE documents SET index_status='indexed' WHERE doc_id=? AND version_no=?",
                (doc_id, version_no),
            )

        self._db.run_write(_write)

    def _activate(self, doc_id: str, version_no: int, index_version: str) -> None:
        """单事务切换 active：旧版本置 superseded，新版本置 active。"""
        def _write(conn) -> None:
            conn.execute(
                "UPDATE chunks SET status='superseded' "
                "WHERE doc_id=? AND version_no<>? AND status IN ('active','indexed')",
                (doc_id, version_no),
            )
            conn.execute(
                "UPDATE chunks SET status='active' "
                "WHERE doc_id=? AND version_no=? AND index_version=? AND status='indexed'",
                (doc_id, version_no, index_version),
            )
            conn.execute(
                "UPDATE documents SET status='published', index_status='active', "
                "active_version=?, index_version=? WHERE doc_id=? AND version_no=?",
                (version_no, index_version, doc_id, version_no),
            )

        self._db.run_write(_write)
        self._log.info("index_activated", doc_id=doc_id, version_no=version_no,
                       index_version=index_version)

    def _mark_failed(self, doc_id: str, version_no: int) -> None:
        """失败留痕：文档与 chunk 都不能停在中间态。"""
        def _write(conn) -> None:
            conn.execute(
                "UPDATE chunks SET status='failed' WHERE doc_id=? AND version_no=? "
                "AND status IN ('pending','indexed')",
                (doc_id, version_no),
            )
            conn.execute(
                "UPDATE documents SET index_status='failed' WHERE doc_id=? AND version_no=?",
                (doc_id, version_no),
            )

        self._db.run_write(_write)


def chunks_to_json(chunks: list[ChunkRecord]) -> str:
    """调试用：把 chunk 列表序列化成 JSON（不落库，仅日志/报告）。"""
    return json.dumps([c.__dict__ for c in chunks], ensure_ascii=False)
