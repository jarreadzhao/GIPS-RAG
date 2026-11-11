"""文档上传与入库接口（API-18，支撑 M0 的 AC-1/2/6/7/11）。

上传只做同步可判定的部分：元数据校验、恶意文件闸门、原件落盘。
解析、切分与建索引交给异步入库写队列（AC-75「写请求进入排队」），
接口返回 202 + `task_id`，处理结果经 `GET /reviews/{task_id}` 查询。
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

import fitz  # PyMuPDF
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from ulid import ULID

from app.core.logging import get_logger
from app.errors import AppError
from app.index.tagging import validate_doc_metadata
from app.ingest.worker import IngestScope

router = APIRouter(prefix="/documents", tags=["documents"])
MAX_UPLOAD_BYTES = 150 * 1024 * 1024  # API-18：单文件上限 150MB


def _scan_file(raw: bytes, filename: str, tmp_root: Path) -> None:
    """最小恶意文件闸门：只收 PDF，拒绝加密文档与带 JS/Launch 动作的 PDF。"""
    if not filename.lower().endswith(".pdf"):
        raise AppError("VALIDATION_FAILED", "只接受 .pdf 文件")
    if not raw.startswith(b"%PDF-"):
        raise AppError("MALWARE_DETECTED", "文件头不是 PDF，拒绝解析")
    tmp = tmp_root / "_scan_tmp.pdf"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(raw)
    try:
        with fitz.open(tmp) as doc:
            if doc.is_encrypted:
                raise AppError("MALWARE_DETECTED", "加密 PDF 拒绝入库")
            for page in doc:
                # 带 JS 或 Launch 动作的 PDF 常见于钓鱼样本，一律拒绝。
                # 链接字典里含 Rect 对象，不能直接 json.dumps，只拼接字符串型字段。
                for link in page.get_links() or []:
                    action = " ".join(
                        str(link.get(key, "")) for key in ("uri", "file", "kind")
                    ).lower()
                    if "javascript" in action or "launch" in action:
                        raise AppError("MALWARE_DETECTED", "PDF 含可执行动作，拒绝解析")
    finally:
        tmp.unlink(missing_ok=True)


def _store_blob(raw: bytes, root: Path) -> tuple[str, Path]:
    """内容寻址存储：同一份字节永远落到同一路径，天然去重。"""
    digest = hashlib.sha256(raw).hexdigest()
    path = root / "blobs" / digest[:2] / f"{digest}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(raw)
    return digest, path


@router.post("")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    doc_id: str = Form(...),
    version_no: int = Form(1),
    corpus_type: str = Form(...),
    doc_type: str = Form(...),
    dept: str = Form(...),
    clearance: str = Form("internal"),
    effective_from: str = Form(...),
    effective_to: str | None = Form(None),
    ocr_approval_ref: str | None = Form(None),
    index_mode: str = Form("full"),
) -> JSONResponse:
    """受理上传并返回入库任务；解析与建索引由写队列串行完成。

    `index_mode=display_only`：只落 SQLite 供引用对照，不写向量库（中文译版等）。
    """
    ctx = request.app.state.ctx
    settings = ctx.settings
    log = get_logger("ingest")

    if index_mode not in ("full", "display_only"):
        raise AppError("VALIDATION_FAILED",
                       "index_mode 只能是 full 或 display_only")

    validate_doc_metadata({
        "corpus_type": corpus_type, "doc_type": doc_type, "dept": dept,
        "clearance": clearance, "effective_from": effective_from,
    })

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise AppError("DOC_TOO_LARGE", f"文件超过 150MB 上限（{len(raw)} 字节）")
    _scan_file(raw, file.filename or "", settings.object_store_dir)
    digest, blob_path = _store_blob(raw, settings.object_store_dir)

    # 同一 doc_id + 同一字节视为幂等上传，直接返回已有状态，不重复排队
    existing = ctx.db.fetch_one(
        "SELECT version_no, status, index_status, active_version FROM documents "
        "WHERE doc_id=? AND file_hash=?",
        (doc_id, digest),
    )
    if existing:
        return JSONResponse(status_code=200, content={
            "doc_id": doc_id, "version_no": existing["version_no"],
            "status": existing["status"], "index_status": existing["index_status"],
            "active_version": existing["active_version"], "duplicate": True,
            "request_id": getattr(request.state, "request_id", ""),
        })

    task_id = "tsk_" + str(ULID())
    scope = IngestScope(doc_id=doc_id, version_no=version_no,
                        index_mode=index_mode,
                        ocr_approval_ref=ocr_approval_ref)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with ctx.db.write() as conn:
        conn.execute(
            """INSERT INTO documents
               (doc_id, version_no, corpus_type, doc_type, dept, clearance,
                effective_from, effective_to, file_hash, file_path, file_size,
                status, index_status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'uploaded','pending',?)""",
            (doc_id, version_no, corpus_type, doc_type, dept, clearance,
             effective_from, effective_to, digest, str(blob_path), len(raw), now),
        )
        # 入库任务复用 review_tasks：supply/inference_scope 是表上的必填列，
        # 这里填本机自建与本机作用域，不代表对外供给档语义
        conn.execute(
            "INSERT INTO review_tasks (task_id, task_type, supply, inference_scope, "
            "scope_json, status, queued_at) VALUES (?,?,?,?,?,?,?)",
            (task_id, "ingest", "self_hosted", "local_single_gpu",
             scope.to_json(), "created", now),
        )

    position = ctx.ingest.submit(task_id, scope)
    log.info("ingest_accepted", task_id=task_id, doc_id=doc_id,
             version_no=version_no, queue_position=position,
             index_mode=index_mode)
    return JSONResponse(status_code=202, content={
        "task_id": task_id, "doc_id": doc_id, "version_no": version_no,
        "status": "uploaded", "file_hash": digest, "duplicate": False,
        "queue_position": position, "status_url": f"/reviews/{task_id}",
        "request_id": getattr(request.state, "request_id", ""),
    })


@router.get("/{doc_id}")
async def get_document(doc_id: str, request: Request) -> dict:
    """返回某文档最新版本状态，供验收脚本核对。"""
    ctx = request.app.state.ctx
    row = ctx.db.fetch_one(
        "SELECT doc_id, version_no, status, index_status, active_version, "
        "parser, page_count, text_layer_ratio FROM documents "
        "WHERE doc_id=? ORDER BY version_no DESC LIMIT 1",
        (doc_id,),
    )
    if row is None:
        raise AppError("VALIDATION_FAILED", f"文档 {doc_id} 不存在")
    return dict(row)
