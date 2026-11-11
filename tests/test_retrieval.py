"""M1 检索与 ACL 验收（AC-15～AC-27）。

全部用例使用内存替身，不依赖 Milvus / GPU；真实链路冒烟另见 reports。
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app.core.embed import EmbeddingBatch
from app.errors import AppError
from app.main import build_context, create_app
from app.retrieve.fusion import rrf_fuse
from app.retrieve.search import Query, RetrievalService, detect_lang
from app.retrieve.structured import StructuredChannel
from app.retrieve.types import Candidate, FusedHit, ProvisionHit, SearchResult


# ---------- 替身 ----------
class FakeEmbedder:
    """确定性嵌入替身。"""

    def encode(self, texts: list[str]) -> EmbeddingBatch:
        return EmbeddingBatch(dense=[[0.0] * 4 for _ in texts],
                              sparse=[{1: 1.0} for _ in texts], tokens=len(texts))

    def unload(self) -> None:
        return None


class SlowEmbedder(FakeEmbedder):
    """用于超时用例。"""

    def encode(self, texts: list[str]) -> EmbeddingBatch:
        time.sleep(0.3)
        return super().encode(texts)


class FakeReranker:
    def score(self, query: str, passages: list[str]) -> list[float]:
        return [1.0 - i * 0.1 for i in range(len(passages))]

    def unload(self) -> None:
        return None


class FakeSearcher:
    """记录过滤表达式，并按脚本返回候选。"""

    def __init__(self, dense: list[Candidate] | None = None,
                 sparse: list[Candidate] | None = None,
                 probe_count: int = 0, delay: float = 0.0) -> None:
        self.dense_results = dense or []
        self.sparse_results = sparse or []
        self.probe_count = probe_count
        self.delay = delay
        self.calls: list[tuple[str, str]] = []

    def dense(self, vector, expr: str, limit: int) -> list[Candidate]:
        self.calls.append(("dense", expr))
        if self.delay:
            time.sleep(self.delay)
        return self.dense_results

    def sparse(self, weights, expr: str, limit: int) -> list[Candidate]:
        self.calls.append(("sparse", expr))
        if self.delay:
            time.sleep(self.delay)
        return self.sparse_results

    def probe(self, expr: str) -> int:
        return self.probe_count


class FakeStructured:
    def __init__(self, results: list[Candidate] | None = None) -> None:
        self.results = results or []
        self.calls = 0

    def search(self, query: str, *, index_versions: list[str],
               depts: list[str], clearances: list[str]) -> list[Candidate]:
        self.calls += 1
        return self.results


# ---------- 数据准备 ----------
def seed_doc(db, *, doc_id: str = "doc1", dept: str = "compliance",
             clearance: str = "internal", lang: str = "en",
             index_version: str = "idx_test") -> None:
    """写入一份 active 文档与一个 chunk，供检索服务读取版本与语种。"""

    def _write(conn) -> None:
        conn.execute(
            "INSERT INTO documents (doc_id, version_no, corpus_type, doc_type, dept, "
            "clearance, effective_from, effective_to, file_hash, file_path, file_size, "
            "page_count, status, index_status, index_version, created_at) "
            "VALUES (?,1,'B_internal','手册',?,?,'2020-01-01T00:00:00Z',NULL,'h','p',1,1,"
            "'published','active',?,'2026-01-01T00:00:00Z')",
            (doc_id, dept, clearance, index_version),
        )
        conn.execute(
            "INSERT INTO chunks (chunk_id, doc_id, version_no, provision_id, chunk_type, "
            "text, token_len, char_start, char_end, page_start, page_end, acl_scope, "
            "corpus_type, lang, index_version, cleaner_version, status) "
            "VALUES (?,?,1,'1.A.1','provision','the firm must keep records',10,0,26,1,1,"
            "?, 'B_internal', ?, ?, 'cleaner-v2', 'active')",
            (f"{doc_id}-c1", doc_id, f'["dept:{dept}","clr:{clearance}"]', lang, index_version),
        )

    db.run_write(_write)


def make_service(settings, db, searcher=None, embedder=None, structured=None
                 ) -> RetrievalService:
    return RetrievalService(db, embedder or FakeEmbedder(), FakeReranker(),
                            searcher or FakeSearcher(), settings,
                            structured or FakeStructured())


def cand(cid: str, pid: str | None, score: float = 1.0,
         channel: str = "dense") -> Candidate:
    return Candidate(chunk_id=cid, provision_id=pid, score=score, channel=channel,
                     page_no=1, text=f"text {cid}", doc_id="doc1")


# ---------- AC-16：缺 ACL 即拒绝且不查库 ----------
def test_acl_missing_rejected_before_any_query(settings, db):
    seed_doc(db)
    searcher = FakeSearcher()
    service = make_service(settings, db, searcher)
    with pytest.raises(AppError) as exc:
        asyncio.run(service.search(Query(text="record keeping", acl_scope=None)))
    assert exc.value.code == "ACL_SCOPE_MISSING"
    assert searcher.calls == []          # 证明未发起任何检索


# ---------- AC-15：越权访问返回空集且归因为 acl_filtered ----------
def test_role_clearance_mapping_blocks_cross_clearance():
    """observer 不能获得 restricted 密级，librarian 可以。"""
    from app.retrieve.acl import scope_from_role

    assert scope_from_role("observer", "risk") == ["dept:risk", "clr:internal"]
    assert scope_from_role("librarian", "risk") == [
        "dept:risk", "clr:internal", "clr:restricted"]
    with pytest.raises(AppError) as exc:
        scope_from_role("ghost", "risk")
    assert exc.value.code == "ACL_DENIED"


def test_observer_cannot_see_other_dept_acl_filtered(settings, db):
    seed_doc(db, dept="compliance", clearance="internal")
    searcher = FakeSearcher(probe_count=3)  # 去掉 ACL 后确有 3 条
    service = make_service(settings, db, searcher)
    result = asyncio.run(service.search(Query(
        text="record keeping", acl_scope=["dept:risk", "clr:internal"])))
    assert result.fused == []
    assert result.empty_reason == "acl_filtered"
    # 过滤必须前置进 search 的 expr，而不是拿到结果后再筛
    assert all('dept in ["risk"]' in expr for _ch, expr in searcher.calls)


def test_empty_scope_returns_acl_filtered_without_query(settings, db):
    seed_doc(db)
    searcher = FakeSearcher(probe_count=3)
    service = make_service(settings, db, searcher)
    result = asyncio.run(service.search(Query(text="record keeping", acl_scope=[])))
    assert result.empty_reason == "acl_filtered"
    assert searcher.calls == []


# ---------- AC-17：跨语言关闭稀疏并标注 ----------
def test_sparse_disabled_on_lang_mismatch(settings, db):
    seed_doc(db, lang="en")
    searcher = FakeSearcher(dense=[cand("c1", "1.A.1")])
    service = make_service(settings, db, searcher)
    result = asyncio.run(service.search(Query(
        text="基金应当如何保存记录？", acl_scope=["dept:compliance", "clr:internal"])))
    assert result.query_lang == "zh"
    assert result.sparse_disabled == "lang_mismatch"
    assert all(name != "sparse" for name, _ in searcher.calls)


def test_detect_lang_boundaries():
    assert detect_lang("record keeping") == "en"
    assert detect_lang("基金应当保存记录") == "zh"
    assert detect_lang("GIPS 2020 合规") == "mixed"
    assert detect_lang("GIPS 2020 合规要求说明") == "zh"


# ---------- AC-18：同一条款只占一席 ----------
def test_same_provision_takes_single_slot(settings, db):
    seed_doc(db)
    dense = [cand("c1", "1.A.1"), cand("c2", "1.A.1"), cand("c3", "1.A.1"),
             cand("c4", "3.B.2"), cand("c5", "5.A.12")]
    service = make_service(settings, db, FakeSearcher(dense=dense))
    result = asyncio.run(service.search(Query(
        text="record keeping", acl_scope=["dept:compliance", "clr:internal"])))
    pids = [h.provision_id for h in result.reranked]
    assert pids.count("1.A.1") == 1
    assert len(pids) == min(5, len(set(pids)))
    assert "3.B.2" in pids and "5.A.12" in pids


def test_back_matter_chunks_do_not_take_clause_slots(settings, db):
    """D-M2-07：无条款号的附录/术语表 chunk 不得占用条款引用槽位。"""
    seed_doc(db)
    dense = [cand("c1", None), cand("c2", "1.A.1"), cand("c3", None)]
    service = make_service(settings, db, FakeSearcher(dense=dense))
    result = asyncio.run(service.search(Query(
        text="verification statement",
        acl_scope=["dept:compliance", "clr:internal"])))
    assert [h.provision_id for h in result.reranked] == ["1.A.1"]


def test_back_matter_only_query_still_returns_chunks(settings, db):
    """全部候选都无条款号时不能直接空答，退回 chunk 级结果。"""
    seed_doc(db)
    dense = [cand("c1", None), cand("c2", None)]
    service = make_service(settings, db, FakeSearcher(dense=dense))
    result = asyncio.run(service.search(Query(
        text="accrual accounting definition",
        acl_scope=["dept:compliance", "clr:internal"])))
    assert result.reranked
    assert all(h.provision_id.startswith("chunk:") for h in result.reranked)


def test_rerank_disabled_keeps_rrf_order(settings, db):
    """测试台关闭重排时直接给 RRF 原序，且不调用重排模型。"""
    seed_doc(db)

    class CountingReranker(FakeReranker):
        def __init__(self) -> None:
            self.calls = 0

        def score(self, query: str, passages: list[str]) -> list[float]:
            self.calls += 1
            return super().score(query, passages)

    reranker = CountingReranker()
    dense = [cand("c1", "1.A.1"), cand("c2", "3.B.2")]
    service = RetrievalService(db, FakeEmbedder(), reranker,
                               FakeSearcher(dense=dense), settings, FakeStructured())
    result = asyncio.run(service.search(Query(
        text="record keeping", acl_scope=["dept:compliance", "clr:internal"],
        rerank=False)))
    assert reranker.calls == 0
    assert result.rerank_skipped is True
    assert [h.provision_id for h in result.reranked] == ["1.A.1", "3.B.2"]


# ---------- AC-19：三通道全失败返回 504，不返回部分结果 ----------
def test_all_channels_timeout_raises_504(settings, db):
    seed_doc(db)
    fast = settings.model_copy(update={"retrieval_channel_timeout_ms": 50,
                                       "retrieval_warmup_timeout_s": 0.05})
    service = make_service(fast, db, FakeSearcher(delay=0.3))
    with pytest.raises(AppError) as exc:
        asyncio.run(service.search(Query(
            text="record keeping", channels=("dense", "sparse"),
            acl_scope=["dept:compliance", "clr:internal"])))
    assert exc.value.code == "UPSTREAM_TIMEOUT"
    assert exc.value.http_status == 504


# ---------- AC-20 / AC-21 / AC-22：SQL 守卫 ----------
def test_sql_guard_rejects_non_whitelist_table(settings, db):
    channel = StructuredChannel(settings.db_path, settings, db)
    with pytest.raises(AppError) as exc:
        channel.execute_sql("SELECT * FROM users")
    assert exc.value.code == "SQL_GUARD_REJECTED"


def test_sql_guard_rejects_multiple_statements(settings, db):
    channel = StructuredChannel(settings.db_path, settings, db)
    with pytest.raises(AppError) as exc:
        channel.execute_sql("SELECT * FROM v_chunk_meta; SELECT * FROM users")
    assert exc.value.code == "SQL_GUARD_REJECTED"


def test_sql_limit_and_truncated(settings, db):
    """白名单关系有 500 行时只返回 200 行并标 truncated（AC-21）。"""
    conn = sqlite3.connect(settings.db_path)
    conn.executescript(
        "DROP VIEW IF EXISTS v_mappings;"
        "CREATE TABLE v_mappings (n INTEGER);"
        "WITH RECURSIVE seq(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM seq WHERE x<500) "
        "INSERT INTO v_mappings SELECT x FROM seq;"
    )
    conn.commit()
    conn.close()

    channel = StructuredChannel(settings.db_path, settings, db)
    result = channel.execute_sql("SELECT * FROM v_mappings")
    assert len(result.rows) == settings.retrieval_sql_max_rows == 200
    assert result.truncated is True

    # 非白名单表仍然被拒
    with pytest.raises(AppError) as exc:
        channel.execute_sql("SELECT * FROM extra_whitelist")
    assert exc.value.code == "SQL_GUARD_REJECTED"


def test_calc_results_attempt_is_audited(settings, db):
    channel = StructuredChannel(settings.db_path, settings, db)
    with pytest.raises(AppError) as exc:
        channel.execute_sql("SELECT * FROM calc_results")
    assert exc.value.code == "SQL_GUARD_REJECTED"
    row = db.fetch_one(
        "SELECT action, target FROM audit_logs WHERE action='SQL_GUARD_REJECTED' "
        "ORDER BY created_at DESC LIMIT 1")
    assert row is not None and "calc_results" in row["target"]


def test_ro_connection_denies_ledger_table(settings, db):
    """AC-26：检索连接读台账表必须鉴权失败（SQLite 无账号，用 authorizer 替代）。"""
    channel = StructuredChannel(settings.db_path, settings, db)
    conn = channel.connect_readonly()
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("SELECT * FROM calc_results").fetchall()
    finally:
        conn.close()


def test_c_corpus_cannot_be_inserted(db):
    """AC-27：C_ledger 在库层就不可写入。"""
    with pytest.raises(sqlite3.IntegrityError):
        db.run_write(lambda conn: conn.execute(
            "INSERT INTO chunks (chunk_id, doc_id, version_no, provision_id, chunk_type, "
            "text, token_len, char_start, char_end, page_start, page_end, acl_scope, "
            "corpus_type, lang, index_version, cleaner_version, status) "
            "VALUES ('x','doc1',1,'1.A.1','provision','t',1,0,1,1,1,'[]','C_ledger','en',"
            "'idx','v','active')"))


# ---------- RRF ----------
def test_rrf_fuses_multiple_channels_by_rank():
    dense = [cand("a", "1.A.1", channel="dense"), cand("b", "1.A.2", channel="dense")]
    sparse = [cand("b", "1.A.2", channel="sparse"), cand("c", "1.A.3", channel="sparse")]
    fused = rrf_fuse([("dense", dense), ("sparse", sparse)], k=60)
    assert [h.chunk_id for h in fused][0] == "b"     # 两路都召回，分数相加排第一
    assert fused[0].channels == ["dense", "sparse"]


# ---------- AC-23 / AC-25：接口层 ----------
class RecordingRetrieval:
    """记录接口层传给检索服务的 acl_scope 与 config。"""

    def __init__(self) -> None:
        self.scopes: list[list[str] | None] = []
        self.rerank_flags: list[bool] = []

    async def search(self, query: Query) -> SearchResult:
        self.scopes.append(query.acl_scope)
        self.rerank_flags.append(query.rerank)
        return SearchResult(
            candidates=[cand("c1", "1.A.1")],
            fused=[FusedHit(chunk_id="c1", provision_id="1.A.1", rrf_score=0.5,
                            channels=["dense"], page_no=1, text="t")],
            reranked=[ProvisionHit(provision_id="1.A.1", score=0.9, rank=1,
                                   chunks=[FusedHit(chunk_id="c1", provision_id="1.A.1",
                                                    rrf_score=0.5, channels=["dense"],
                                                    page_no=1, text="t")])],
            index_version="idx_test", query_lang="en",
        )


def _api_context(settings, recorder: RecordingRetrieval):
    ctx = build_context(settings)
    ctx.retrieval = recorder
    return create_app(ctx)


def test_api_ignores_forged_acl_scope_in_query_string(settings):
    recorder = RecordingRetrieval()
    app = _api_context(settings, recorder)
    with TestClient(app) as client:
        resp = client.get(
            "/provisions/search",
            params={"q": "record keeping", "acl_scope": '["dept:risk","clr:restricted"]'},
            headers={"X-Role": "observer", "X-Dept": "compliance"})
    assert resp.status_code == 200
    # 只采用网关注入的角色映射；query string 的伪造值被忽略
    assert recorder.scopes == [["dept:compliance", "clr:internal"]]


def test_api_missing_scope_returns_400(settings):
    recorder = RecordingRetrieval()
    app = _api_context(settings, recorder)
    with TestClient(app) as client:
        resp = client.get("/provisions/search", params={"q": "record keeping"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "ACL_SCOPE_MISSING"
    assert recorder.scopes == []


def test_api_17_echoes_config_exactly(settings):
    recorder = RecordingRetrieval()
    app = _api_context(settings, recorder)
    payload = {"query": "record keeping", "role_view": "admin",
               "config": {"strategy": "dense", "top_k": 7, "rerank": False}}
    with TestClient(app) as client:
        resp = client.post("/retrieval/test", json=payload,
                           headers={"X-Role": "admin", "X-Dept": "compliance"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["config_echo"] == payload["config"]
    assert recorder.rerank_flags == [False]
    assert body["acl_scope_used"] == [
        "dept:compliance", "clr:internal", "clr:restricted"]


def test_api_17_forged_acl_scope_in_body_is_ignored(settings):
    """AC-25：请求体伪造 acl_scope 必须被网关身份覆盖。"""
    recorder = RecordingRetrieval()
    app = _api_context(settings, recorder)
    payload = {"query": "record keeping",
               "acl_scope": ["dept:risk", "clr:restricted"],
               "config": {}}
    with TestClient(app) as client:
        resp = client.post("/retrieval/test", json=payload,
                           headers={"X-Role": "observer", "X-Dept": "compliance"})
    assert resp.status_code == 200
    assert recorder.scopes == [["dept:compliance", "clr:internal"]]


def test_api_17_role_view_forbidden_for_observer(settings):
    recorder = RecordingRetrieval()
    app = _api_context(settings, recorder)
    with TestClient(app) as client:
        resp = client.post("/retrieval/test",
                           json={"query": "record", "role_view": "admin"},
                           headers={"X-Role": "observer", "X-Dept": "compliance"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "ACL_DENIED"
