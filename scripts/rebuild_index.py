"""索引重建演练（AC-73）。

回答的问题：Milvus 里的向量丢了，能不能只靠**对象存储里的原件**把索引重建成
和原来一样，以及要花多久（RTO 判据 ≤ 4 小时）。

三个阶段分开计时，因为瓶颈通常不在同一处：
    解析（parse）→ 清洗 + 切分（clean/chunk）→ 嵌入 + 写入 + 条数核对（build）

用法：
    python scripts/rebuild_index.py --probe
    python scripts/rebuild_index.py --drop
    python scripts/rebuild_index.py --rebuild --out var/reports/rebuild_drill.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.core.db import get_database  # noqa: E402
from app.core.egress import build_egress_gateway  # noqa: E402
from app.core.embed import build_embedder  # noqa: E402
from app.core.gpu_pool import GPUPool  # noqa: E402
from app.index.builder import (ChunkRecord, IndexBuilder, evaluate_quality,  # noqa: E402
                               build_milvus_writer)
from app.index.tagging import build_acl_scope  # noqa: E402
from app.parse.cleaner import clean  # noqa: E402
from app.parse.chunker import chunk_document, make_chunk_id  # noqa: E402
from app.parse.pdf_text import ParsedDocument  # noqa: E402
from app.parse.router import DocumentRouter, persist_page_texts  # noqa: E402


def _lang_of(text: str) -> str:
    """与 app/api/documents.py 同一判定：>30% 汉字为 zh，≤5% 为 en，其余 mixed。"""
    if not text:
        return "en"
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    ratio = cjk / len(text)
    if ratio >= 0.3:
        return "zh"
    if ratio <= 0.05:
        return "en"
    return "mixed"


def _client():
    from pymilvus import MilvusClient

    return MilvusClient(get_settings().milvus_uri)


def probe(doc_id: str, index_version: str | None) -> dict:
    """读取 Milvus 侧条数；集合不存在时返回 missing，不抛异常。"""
    settings = get_settings()
    client = _client()
    collections = client.list_collections()
    result: dict = {"collections": collections,
                    "collection": settings.milvus_collection,
                    "collection_exists": settings.milvus_collection in collections}
    if not result["collection_exists"]:
        return result
    result["row_count_total"] = client.query(
        collection_name=settings.milvus_collection, filter="",
        output_fields=["count(*)"])[0]["count(*)"]
    result["row_count_doc"] = client.query(
        collection_name=settings.milvus_collection,
        filter=f'doc_id == "{doc_id}"', output_fields=["count(*)"])[0]["count(*)"]
    if index_version:
        result["row_count_index_version"] = client.query(
            collection_name=settings.milvus_collection,
            filter=f'index_version == "{index_version}"',
            output_fields=["count(*)"])[0]["count(*)"]
    return result


def drop() -> dict:
    """删除整个集合，模拟向量库丢失（SQLite 与对象存储不动）。"""
    settings = get_settings()
    client = _client()
    if settings.milvus_collection not in client.list_collections():
        return {"dropped": False, "reason": "集合本来就不存在"}
    client.drop_collection(settings.milvus_collection)
    return {"dropped": True,
            "collections_after": client.list_collections()}


def rebuild(doc_id: str, out: Path) -> dict:
    """由对象存储中的原件重建索引，逐阶段计时。"""
    settings = get_settings()
    settings.object_store_dir.mkdir(parents=True, exist_ok=True)
    db = get_database(settings.db_path)
    db.init_schema()

    row = db.fetch_one(
        "SELECT doc_id, version_no, file_hash, file_path, corpus_type, doc_type, "
        "dept, clearance, index_version FROM documents "
        "WHERE doc_id=? ORDER BY version_no DESC LIMIT 1", (doc_id,))
    if row is None:
        raise SystemExit(f"documents 里没有 {doc_id}，无法重建")
    version_no = int(row["version_no"])
    pdf_path = str(row["file_path"])
    if not Path(pdf_path).exists():
        raise SystemExit(f"原件不存在：{pdf_path}（对象存储被删则无法重建）")

    pool = GPUPool(co_resident_slots=("embed", "rerank"))
    timings: dict[str, float] = {}
    try:
        embedder = build_embedder(settings, pool)
        gateway = build_egress_gateway(settings, db)
        doc_router = DocumentRouter(settings, gateway)
        writer = build_milvus_writer(settings)
        builder = IndexBuilder(db, embedder, writer, settings)

        t0 = time.perf_counter()
        outcome = doc_router.parse(pdf_path, doc_id=doc_id, version_no=version_no,
                                   corpus_type=str(row["corpus_type"]))
        timings["parse_s"] = round(time.perf_counter() - t0, 3)
        if not outcome.ok:
            raise SystemExit(f"解析失败：{[f.message for f in outcome.failures]}")

        t0 = time.perf_counter()
        cleaned = clean(outcome.parsed, version=settings.cleaner_version)
        persist_page_texts(settings.object_store_dir, doc_id, version_no,
                           cleaned.pages_text)
        drafts = chunk_document(
            ParsedDocument(pages_text=cleaned.pages_text,
                           blocks=cleaned.blocks, report=outcome.parsed.report),
            strategy=settings.chunker_strategy,
            lang=_lang_of(" ".join(b.text for b in cleaned.blocks)),
            target_chars=settings.chunker_target_chars,
            max_chars=settings.chunker_max_chars,
            overlap=settings.chunker_overlap,
        )
        acl_scope = build_acl_scope(str(row["dept"]), str(row["clearance"]))
        chunks = [ChunkRecord(
            chunk_id=make_chunk_id(doc_id, version_no, d.page_no,
                                   d.char_start, d.char_end),
            doc_id=doc_id, version_no=version_no,
            provision_id=d.provision_id, chunk_type=d.chunk_type, text=d.text,
            token_len=max(1, len(d.text) // 2), char_start=d.char_start,
            char_end=d.char_end, page_start=d.page_no, page_end=d.page_no,
            acl_scope=acl_scope, corpus_type=str(row["corpus_type"]),
            pair_id=d.pair_id, lang=d.lang, index_version="pending",
            cleaner_version=cleaned.version, dept=str(row["dept"]),
            clearance=str(row["clearance"]),
        ) for d in drafts]
        timings["clean_chunk_s"] = round(time.perf_counter() - t0, 3)

        quality = evaluate_quality(
            page_count=outcome.parsed.report.page_count if outcome.parsed.report else 0,
            failed_pages=len(outcome.failures), chunk_count=len(chunks),
            empty_chunks=sum(1 for c in chunks if not c.text.strip()),
            table_pages=0, table_misaligned=0, settings=settings)
        if not quality.passed:
            raise SystemExit(f"质量闸门未过：{quality.failures}")

        t0 = time.perf_counter()
        result = builder.build(chunks, doc_id=doc_id, version_no=version_no,
                               file_hash=str(row["file_hash"]))
        timings["build_s"] = round(time.perf_counter() - t0, 3)
    finally:
        pool.shutdown()

    report = {
        "doc_id": doc_id,
        "version_no": version_no,
        "source_pdf": pdf_path,
        "index_version_before": row["index_version"],
        "index_version_after": result.index_version,
        "index_version_unchanged": result.index_version == row["index_version"],
        "chunk_count": result.chunk_count,
        "milvus_count": result.milvus_count,
        "state": result.state,
        "timings_s": timings,
        "total_s": round(sum(timings.values()), 3),
        "probe_after": probe(doc_id, result.index_version),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="索引重建演练（AC-73）")
    parser.add_argument("--doc-id", default="gips2020")
    parser.add_argument("--index-version", default=None)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--drop", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--out", default="var/reports/rebuild_drill.json")
    args = parser.parse_args()

    if args.probe:
        print(json.dumps(probe(args.doc_id, args.index_version),
                         ensure_ascii=False, indent=2))
    if args.drop:
        print(json.dumps(drop(), ensure_ascii=False, indent=2))
    if args.rebuild:
        rebuild(args.doc_id, Path(args.out))
    if not (args.probe or args.drop or args.rebuild):
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
