"""FastAPI 入口（T-0-01）。

启动顺序很重要：
    1. 配置校验（含禁用切分策略）——失败即拒绝启动
    2. SQLite 建表与触发器
    3. 回收上次崩溃遗留的中间态索引
    4. 组装显存池与模型适配器（惰性加载，启动不占显存）
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from ulid import ULID

from app.api import documents, retrieval, reviews
from app.config import Settings, get_settings
from app.core.db import Database, get_database
from app.core.embed import Embedder, build_embedder
from app.core.egress import EgressGateway, build_egress_gateway
from app.core.gpu_pool import GPUPool
from app.core.llm import LLMBackend, build_llm_backend
from app.core.logging import configure_logging, get_logger
from app.core.rerank import Reranker, build_reranker
from app.errors import AppError, app_error_handler
from app.index.builder import IndexBuilder, MilvusWriter, build_milvus_writer
from app.index.pairing import pairing_status
from app.ingest.worker import IngestWorker
from app.parse.router import DocumentRouter
from app.reason.orchestrator import LLMChainGenerator, ReviewOrchestrator
from app.retrieve.search import RetrievalService
from app.retrieve.vector import VectorSearcher, build_searcher

VERSION = "0.1.0-M2"


@dataclass
class AppContext:
    """进程级依赖容器，挂在 app.state.ctx 上。"""

    settings: Settings
    db: Database
    pool: GPUPool
    llm: LLMBackend
    embedder: Embedder
    reranker: Reranker
    gateway: EgressGateway
    router: DocumentRouter
    builder: IndexBuilder
    writer: MilvusWriter
    searcher: VectorSearcher
    retrieval: RetrievalService
    review: ReviewOrchestrator
    ingest: IngestWorker


def _check_bilingual_pairing(db: Database) -> None:
    """启动自检：中文侧有配对键、英文侧却全空，说明英文重入库把 pair_id 冲掉了。

    配对键只决定引用里带不带中文对照，不影响检索与答案，因此告警而不阻断启动。
    """
    stats = pairing_status(db)
    if stats["zh_paired"] and not stats["en_paired"]:
        get_logger("ingest").warning(
            "bilingual_pairing_lost",
            en_total=stats["en_total"], en_paired=stats["en_paired"],
            zh_paired=stats["zh_paired"],
            hint="英文侧 pair_id 全空，引用不会带中文对照；"
                 "重跑 scripts/pair_bilingual.py 可恢复")


def build_context(settings: Settings | None = None,
                  writer: MilvusWriter | None = None) -> AppContext:
    """组装依赖；测试可注入自定义 MilvusWriter。"""
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    settings.object_store_dir.mkdir(parents=True, exist_ok=True)
    db = get_database(settings.db_path)
    db.init_schema()

    # 检索一次请求要先后用嵌入与重排：允许两者共存，避免相互卸载导致
    # 每次请求都触发模型重载（生成槽位仍会清空它们，8GB 上限不受影响）
    pool = GPUPool(co_resident_slots=("embed", "rerank"))
    llm = build_llm_backend(settings, pool)
    embedder = build_embedder(settings, pool)
    reranker = build_reranker(settings, pool)
    gateway = build_egress_gateway(settings, db)
    doc_router = DocumentRouter(settings, gateway)
    milvus_writer = writer or build_milvus_writer(settings)
    builder = IndexBuilder(db, embedder, milvus_writer, settings)
    builder.recover_incomplete()
    _check_bilingual_pairing(db)
    searcher = build_searcher(settings)
    retrieval = RetrievalService(db, embedder, reranker, searcher, settings)
    review = ReviewOrchestrator(db, settings,
                                generator=LLMChainGenerator(llm),
                                retrieval=retrieval)
    ingest = IngestWorker(settings, db, doc_router, builder)
    return AppContext(settings=settings, db=db, pool=pool, llm=llm,
                      embedder=embedder, reranker=reranker, gateway=gateway,
                      router=doc_router, builder=builder, writer=milvus_writer,
                      searcher=searcher, retrieval=retrieval, review=review,
                      ingest=ingest)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建上下文并拉起入库写队列，退出时停队列、释放显存。"""
    if not hasattr(app.state, "ctx"):
        app.state.ctx = build_context()
    app.state.ctx.ingest.start()
    try:
        yield
    finally:
        app.state.ctx.ingest.stop()
        app.state.ctx.pool.shutdown()


def create_app(context: AppContext | None = None) -> FastAPI:
    """创建应用；测试可直接传入已组装的 context。"""
    app = FastAPI(title="GIPS 审查 RAG", version=VERSION, lifespan=lifespan)
    if context is not None:
        app.state.ctx = context

    app.add_exception_handler(AppError, app_error_handler)
    # 单机联调：放行前端跨域（前端 :8765 对后端 :8011）。
    # allow_credentials=False 与 allow_origins=["*"] 不能同时设，这里选了 *, 不用 cookie。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_headers=["*"],
        allow_methods=["*"],
        allow_credentials=False,
    )
    app.include_router(documents.router)
    app.include_router(retrieval.router)
    app.include_router(reviews.router)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        """为每个请求生成 ULID 并回写响应头，日志与错误体共用同一个 ID。"""
        request.state.request_id = str(ULID())
        started = time.perf_counter()
        response = await call_next(request)
        elapsed = (time.perf_counter() - started) * 1000
        response.headers["X-Request-Id"] = request.state.request_id
        get_logger("access").info("http_request", method=request.method,
                                  path=request.url.path,
                                  status=response.status_code,
                                  elapsed_ms=round(elapsed, 2),
                                  request_id=request.state.request_id)
        return response

    @app.get("/healthz")
    async def healthz() -> dict:
        """存活探针：不依赖 Milvus/Redis，避免基础设施抖动导致误判进程死亡。"""
        ctx = app.state.ctx
        return {"status": "ok", "version": VERSION,
                "llm_backend": ctx.settings.llm_backend,
                "resident_models": ctx.pool.resident()}

    return app


app = create_app()
