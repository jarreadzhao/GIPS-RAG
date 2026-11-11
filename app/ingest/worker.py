"""异步入库写队列（AC-75「写请求进入排队」的落点）。

上传接口只做校验、恶意文件闸门与原件落盘，然后返回 202 + `task_id`；
解析、清洗、切分、建索引由单写队列串行执行。任务载体复用 `review_tasks`
（`task_type='ingest'`），不新建表——状态机与查询接口因此不必再实现一套。

已知债务：`review_tasks` 混入入库任务后，任何「审查任务数」统计都必须按
`task_type` 过滤，否则会把入库也算进去（CHG-20261110-11 已记录）。

失败语义：任务终态 `failed` 时 `business_code` 记失败原因（OCR 失败统一记
`ocr_failed`），同时把 `documents.status` 置 `failed`，不得静默丢单。
"""
from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

from app.config import Settings
from app.core.db import Database
from app.core.logging import get_logger
from app.errors import AppError
from app.index.builder import (ChunkRecord, DISPLAY_ONLY_INDEX, IndexBuilder,
                               evaluate_quality)
from app.index.tagging import build_acl_scope, validate_chunk_metadata
from app.parse.cleaner import clean
from app.parse.chunker import chunk_document, make_chunk_id
from app.parse.pdf_text import ParsedDocument
from app.parse.router import DocumentRouter, persist_page_texts

_STOP = object()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True)
class IngestScope:
    """入库任务的载荷；落 `review_tasks.scope_json`，重启后可据此重排队。"""

    doc_id: str
    version_no: int
    index_mode: str = "full"
    ocr_approval_ref: str | None = None

    def to_json(self) -> str:
        return json.dumps({"kind": "ingest", **asdict(self)},
                          ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "IngestScope":
        data = json.loads(raw)
        return cls(doc_id=data["doc_id"], version_no=int(data["version_no"]),
                   index_mode=data.get("index_mode", "full"),
                   ocr_approval_ref=data.get("ocr_approval_ref"))


def _lang_of(text: str) -> str:
    """按汉字占比判定语种：>30% 中文，<5% 英文，其余 mixed。"""
    if not text:
        return "en"
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    ratio = cjk / len(text)
    if ratio >= 0.3:
        return "zh"
    if ratio <= 0.05:
        return "en"
    return "mixed"


def run_ingest(settings: Settings, db: Database, doc_router: DocumentRouter,
               builder: IndexBuilder, scope: IngestScope) -> dict[str, Any]:
    """执行一份文档的解析、清洗、切分与索引构建。"""
    log = get_logger("ingest")
    doc = db.fetch_one(
        "SELECT * FROM documents WHERE doc_id=? AND version_no=?",
        (scope.doc_id, scope.version_no),
    )
    if doc is None:
        raise AppError("VALIDATION_FAILED",
                       f"文档 {scope.doc_id} v{scope.version_no} 不存在，任务作废")

    doc_id, version_no = scope.doc_id, scope.version_no
    corpus_type, dept, clearance = doc["corpus_type"], doc["dept"], doc["clearance"]
    blob_path, digest = doc["file_path"], doc["file_hash"]

    outcome = doc_router.parse(
        blob_path, doc_id=doc_id, version_no=version_no,
        corpus_type=corpus_type, ocr_approval_ref=scope.ocr_approval_ref,
    )
    db.run_write(lambda conn: conn.execute(
        "UPDATE documents SET parser=?, text_layer_ratio=?, page_count=?, status=? "
        "WHERE doc_id=? AND version_no=?",
        (outcome.parser,
         outcome.parsed.report.coverage if outcome.parsed.report else 0.0,
         outcome.parsed.report.page_count if outcome.parsed.report else 0,
         "parsed" if outcome.ok else "failed", doc_id, version_no),
    ))

    if not outcome.ok:
        log.error("ingest_parse_failed", doc_id=doc_id, version_no=version_no,
                  failed_pages=[f.page_no for f in outcome.failures])
        raise AppError(
            "UPSTREAM_TIMEOUT",
            "解析失败，已标 ocr_failed 并进入人工复核队列",
            details={"failed_pages": [asdict(f) for f in outcome.failures]},
        )

    cleaned = clean(outcome.parsed, version=settings.cleaner_version)
    # 落盘与切分都必须用清洗后的规范字符流：偏移是按删块后的文本重算的，
    # 用解析期的旧文本做切片会对不上
    persist_page_texts(settings.object_store_dir, doc_id, version_no,
                       cleaned.pages_text)
    drafts = chunk_document(
        ParsedDocument(pages_text=cleaned.pages_text,
                       blocks=cleaned.blocks, report=outcome.parsed.report),
        strategy=settings.chunker_strategy,
        # 语种按全文判定：封面与页眉是英文，只取前 20 块会把中文译本判成 mixed
        lang=_lang_of(" ".join(b.text for b in cleaned.blocks)),
        target_chars=settings.chunker_target_chars,
        max_chars=settings.chunker_max_chars,
        overlap=settings.chunker_overlap,
    )

    acl_scope = build_acl_scope(dept, clearance)
    chunks: list[ChunkRecord] = []
    for d in drafts:
        chunk_id = make_chunk_id(doc_id, version_no, d.page_no, d.char_start,
                                 d.char_end)
        chunks.append(ChunkRecord(
            chunk_id=chunk_id, doc_id=doc_id, version_no=version_no,
            provision_id=d.provision_id, chunk_type=d.chunk_type, text=d.text,
            token_len=max(1, len(d.text) // 2), char_start=d.char_start,
            char_end=d.char_end, page_start=d.page_no, page_end=d.page_no,
            acl_scope=acl_scope, corpus_type=corpus_type, pair_id=d.pair_id,
            lang=d.lang, index_version="pending", cleaner_version=cleaned.version,
            dept=dept, clearance=clearance,
        ))

    bad = [c.chunk_id for c in chunks if validate_chunk_metadata(asdict(c))]
    if bad:
        raise AppError("VALIDATION_FAILED", f"{len(bad)} 个 chunk 元数据不全，拒绝入库",
                       details={"chunk_ids": bad[:10]})

    quality = evaluate_quality(
        page_count=outcome.parsed.report.page_count if outcome.parsed.report else 0,
        failed_pages=len(outcome.failures), chunk_count=len(chunks),
        empty_chunks=sum(1 for c in chunks if not c.text.strip()), table_pages=0,
        table_misaligned=0, settings=settings,
    )
    if not quality.passed:
        raise AppError("VALIDATION_FAILED", "解析质量未过闸门，拒绝建索引",
                       details={"failures": quality.failures})

    parse_report = {
        "parser": outcome.parser,
        "ocr_pages": list(outcome.ocr_pages),
        "text_layer_ratio": outcome.parsed.report.coverage if outcome.parsed.report else 0,
        "dropped": dict(cleaned.dropped),
    }
    quality_report = {"passed": quality.passed,
                      "parse_success_rate": quality.parse_success_rate,
                      "empty_chunk_rate": quality.empty_chunk_rate}

    if scope.index_mode == "display_only":
        count = builder.build_display_only(chunks, doc_id=doc_id,
                                           version_no=version_no)
        return {"doc_id": doc_id, "version_no": version_no, "status": "published",
                "index_status": DISPLAY_ONLY_INDEX, "index_version": DISPLAY_ONLY_INDEX,
                "chunk_count": count, "milvus_count": 0,
                "parse_report": parse_report, "quality_report": quality_report}

    result = builder.build(chunks, doc_id=doc_id, version_no=version_no,
                           file_hash=digest)
    return {"doc_id": doc_id, "version_no": version_no, "status": "published",
            "index_status": result.state, "index_version": result.index_version,
            "chunk_count": result.chunk_count, "milvus_count": result.milvus_count,
            "parse_report": parse_report, "quality_report": quality_report}


class IngestWorker:
    """单线程写队列：显存与 SQLite 写锁都只有一份，串行即正确语义。"""

    def __init__(self, settings: Settings, db: Database,
                 doc_router: DocumentRouter, builder: IndexBuilder) -> None:
        self._settings = settings
        self._db = db
        self._router = doc_router
        self._builder = builder
        self._log = get_logger("ingest")
        self._queue: queue.Queue[Any] = queue.Queue()
        self._lock = threading.Lock()
        self._pending = 0
        self._thread: threading.Thread | None = None

    # ---------- 生命周期 ----------
    def start(self) -> None:
        """启动消费线程，并把崩溃前未开始的任务重新排队。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="ingest-worker",
                                            daemon=True)
            self._thread.start()
        self._recover()

    def stop(self) -> None:
        """停止消费线程；正在处理的任务随进程退出，重启时按中断处理。"""
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is None:
            return
        self._queue.put(_STOP)
        thread.join(timeout=10)

    def submit(self, task_id: str, scope: IngestScope) -> int:
        """入队并返回排队位次：0 表示没有其他任务在等，>0 表示前面还有几个。"""
        with self._lock:
            position = self._pending
            self._pending += 1
        self._queue.put((task_id, scope))
        return position

    def pending(self) -> int:
        """当前等待中的入库任务数，供 `GET /reviews/{task_id}` 回传位次。"""
        with self._lock:
            return self._pending

    # ---------- 消费 ----------
    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                task_id, scope = item
                with self._lock:
                    self._pending = max(0, self._pending - 1)
                self._run(task_id, scope)
            finally:
                self._queue.task_done()

    def _run(self, task_id: str, scope: IngestScope) -> None:
        self._db.run_write(lambda conn: conn.execute(
            "UPDATE review_tasks SET status='running', started_at=? "
            "WHERE task_id=? AND status='created'", (_now(), task_id)))
        self._log.info("ingest_task_started", task_id=task_id,
                       doc_id=scope.doc_id, version_no=scope.version_no)
        try:
            result = run_ingest(self._settings, self._db, self._router,
                                self._builder, scope)
        except AppError as exc:
            code = "ocr_failed" if exc.code == "UPSTREAM_TIMEOUT" else exc.code
            self._fail(task_id, scope, code, exc.message)
        except Exception as exc:  # 兜底：单个任务异常不能让消费线程退出
            self._log.error("ingest_task_crashed", task_id=task_id,
                            doc_id=scope.doc_id, reason=str(exc)[:200])
            self._fail(task_id, scope, "INTERNAL_ERROR", f"入库任务异常：{exc}")
        else:
            self._db.run_write(lambda conn: conn.execute(
                "UPDATE review_tasks SET status='succeeded', finished_at=? "
                "WHERE task_id=?", (_now(), task_id)))
            self._log.info("ingest_task_succeeded", task_id=task_id,
                           doc_id=scope.doc_id, index_status=result["index_status"],
                           chunk_count=result["chunk_count"])

    def _fail(self, task_id: str, scope: IngestScope, code: str,
              message: str) -> None:
        """终态失败：任务落 business_code，文档置 failed（已发布的版本不回退）。"""
        def _write(conn) -> None:
            conn.execute(
                "UPDATE review_tasks SET status='failed', finished_at=?, "
                "business_code=? WHERE task_id=?", (_now(), code, task_id))
            conn.execute(
                "UPDATE documents SET status='failed' WHERE doc_id=? AND version_no=? "
                "AND status <> 'published'", (scope.doc_id, scope.version_no))

        self._db.run_write(_write)
        self._log.error("ingest_task_failed", task_id=task_id, doc_id=scope.doc_id,
                        version_no=scope.version_no, business_code=code,
                        reason=message[:200])

    # ---------- 恢复 ----------
    def _recover(self) -> None:
        """`created` 的重新入队；`running` 的说明进程在任务中途退出，判失败。"""
        rows = self._db.fetch_all(
            "SELECT task_id, scope_json FROM review_tasks "
            "WHERE task_type='ingest' AND status='created'", ())
        for row in rows:
            try:
                scope = IngestScope.from_json(row["scope_json"])
            except (KeyError, ValueError, TypeError):
                self._db.run_write(lambda conn, tid=row["task_id"]: conn.execute(
                    "UPDATE review_tasks SET status='failed', finished_at=?, "
                    "business_code='SCOPE_INVALID' WHERE task_id=?", (_now(), tid)))
                continue
            self.submit(row["task_id"], scope)
        if rows:
            self._log.info("ingest_tasks_requeued", count=len(rows))

        running = self._db.fetch_all(
            "SELECT task_id, scope_json FROM review_tasks "
            "WHERE task_type='ingest' AND status='running'", ())
        for row in running:
            try:
                scope = IngestScope.from_json(row["scope_json"])
            except (KeyError, ValueError, TypeError):
                continue
            self._fail(row["task_id"], scope, "WORKER_RESTARTED",
                       "服务重启，任务中断，需重新上传或人工复核")
