"""Milvus 集合定义（T-0-12 前半）。

设计取舍：
    - 稠密与稀疏放在同一个集合的同一行，避免两路检索各查一次再对齐 ID。
    - acl_scope 用 VARCHAR 存 JSON 数组并建 INVERTED 索引；
      ACL 必须能作为 search 的 filter 前置条件，不能拿到结果再过滤。
    - 所有标量字段都建 INVERTED 索引，供 text-to-SQL 白名单之外的检索过滤。
"""
from __future__ import annotations

from app.core.logging import get_logger

_log = get_logger("ingest")

# 集合字段声明，供建表与单测共同引用
SCALAR_FIELDS = (
    "doc_id", "version_no", "provision_id", "corpus_type", "dept", "clearance",
    "chunk_type", "lang", "pair_id", "index_version", "cleaner_version",
    "page_start", "page_end", "char_start", "char_end", "acl_scope", "text",
)


def build_schema(dim: int = 1024):
    """构造 CollectionSchema；延迟导入 pymilvus，便于无 Milvus 环境跑单测。"""
    from pymilvus import CollectionSchema, DataType, FieldSchema

    fields = [
        FieldSchema("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64),
        FieldSchema("doc_id", DataType.VARCHAR, max_length=64),
        FieldSchema("version_no", DataType.INT64),
        FieldSchema("provision_id", DataType.VARCHAR, max_length=32),
        FieldSchema("corpus_type", DataType.VARCHAR, max_length=16),
        FieldSchema("dept", DataType.VARCHAR, max_length=32),
        FieldSchema("clearance", DataType.VARCHAR, max_length=16),
        FieldSchema("chunk_type", DataType.VARCHAR, max_length=16),
        FieldSchema("lang", DataType.VARCHAR, max_length=8),
        FieldSchema("pair_id", DataType.VARCHAR, max_length=32),
        FieldSchema("index_version", DataType.VARCHAR, max_length=64),
        FieldSchema("cleaner_version", DataType.VARCHAR, max_length=32),
        FieldSchema("page_start", DataType.INT64),
        FieldSchema("page_end", DataType.INT64),
        FieldSchema("char_start", DataType.INT64),
        FieldSchema("char_end", DataType.INT64),
        FieldSchema("acl_scope", DataType.VARCHAR, max_length=256),
        FieldSchema("text", DataType.VARCHAR, max_length=8192),
        FieldSchema("dense", DataType.FLOAT_VECTOR, dim=dim),
        FieldSchema("sparse", DataType.SPARSE_FLOAT_VECTOR),
    ]
    return CollectionSchema(fields=fields, description="GIPS 条款与手册 chunk")


def build_index_params():
    """稠密用 HNSW/COSINE；稀疏用 INVERTED；标量全部 INVERTED。"""
    from pymilvus import DataType

    dense = {
        "index_type": "HNSW",
        "metric_type": "COSINE",
        "params": {"M": 16, "efConstruction": 200},
    }
    # 稀疏向量只能建稀疏专用索引（INVERTED 是标量索引，用在稀疏向量上会被服务端拒绝）
    sparse = {"index_type": "SPARSE_INVERTED_INDEX", "metric_type": "IP"}
    scalar = {"index_type": "INVERTED"}
    return {
        "dense": ("dense", dense),
        "sparse": ("sparse", sparse),
        "scalars": [
            ("doc_id", scalar), ("provision_id", scalar), ("corpus_type", scalar),
            ("dept", scalar), ("clearance", scalar), ("index_version", scalar),
            ("acl_scope", scalar), ("pair_id", scalar), ("lang", scalar),
        ],
        "dtype_sparse": DataType.SPARSE_FLOAT_VECTOR,
    }


def ensure_collection(client, name: str, dim: int = 1024) -> None:
    """集合不存在则创建并建索引，存在则只加载。"""
    if client.has_collection(name):
        _log.info("milvus_collection_exists", collection=name)
        client.load_collection(name)
        return
    schema = build_schema(dim)
    index_params = client.prepare_index_params()
    index_params.add_index(field_name="dense", index_type="HNSW",
                           metric_type="COSINE", params={"M": 16, "efConstruction": 200})
    index_params.add_index(field_name="sparse", index_type="SPARSE_INVERTED_INDEX",
                           metric_type="IP")
    for field in ("doc_id", "provision_id", "corpus_type", "dept", "clearance",
                  "index_version", "acl_scope", "pair_id", "lang"):
        index_params.add_index(field_name=field, index_type="INVERTED")
    client.create_collection(collection_name=name, schema=schema,
                             index_params=index_params)
    client.load_collection(name)
    _log.info("milvus_collection_created", collection=name, dim=dim)
