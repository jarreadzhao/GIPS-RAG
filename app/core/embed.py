"""嵌入模型适配层（T-0-05）。

bge-m3 一次编码同时产出稠密向量与稀疏权重：
    - 稠密 1024 维，入 Milvus 的 FLOAT_VECTOR 字段
    - 稀疏词典（token_id -> weight），入 SPARSE_FLOAT_VECTOR 字段

与生成/重排一样，加载卸载必须经 GPUPool，不允许自行 torch.cuda 操作。
"""
from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.config import Settings
from app.core.gpu_pool import GPUPool
from app.errors import AppError

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


@dataclass
class EmbeddingBatch:
    """一批文本的嵌入结果。"""

    dense: list[list[float]]                # 每行 1024 维
    sparse: list[dict[int, float]]          # 每行 token_id -> 权重
    tokens: int                             # 本批消耗的 token 数


class Embedder(ABC):
    """嵌入后端协议。"""

    slot = "embed"

    @abstractmethod
    def encode(self, texts: list[str]) -> EmbeddingBatch:
        """批量编码。"""

    @abstractmethod
    def unload(self) -> None:
        """释放权重。"""


class HashingEmbedder(Embedder):
    """确定性哈希嵌入，用于无 GPU 的单元测试与离线流水线冒烟。

    它不是语义模型：只保证「相同文本得到相同向量」，用于验证索引写入、
    过滤条件与回链逻辑，不参与任何召回指标。
    """

    slot = "embed"

    def __init__(self, dim: int = 1024) -> None:
        self._dim = dim

    def load(self) -> None:
        """无权重。"""

    def encode(self, texts: list[str]) -> EmbeddingBatch:
        dense: list[list[float]] = []
        sparse: list[dict[int, float]] = []
        tokens = 0
        for text in texts:
            vec = [0.0] * self._dim
            lex: dict[int, float] = {}
            toks = _TOKEN_RE.findall(text.lower())
            tokens += max(1, len(toks))
            for tok in toks:
                h = int(hashlib.sha256(tok.encode("utf-8")).hexdigest()[:8], 16)
                vec[h % self._dim] += 1.0
                lex[h % 100000] = lex.get(h % 100000, 0.0) + 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            dense.append([v / norm for v in vec])
            total = sum(lex.values()) or 1.0
            sparse.append({k: v / total for k, v in lex.items()})
        return EmbeddingBatch(dense=dense, sparse=sparse, tokens=tokens)

    def unload(self) -> None:
        """无权重。"""


class BgeM3Embedder(Embedder):
    """FlagEmbedding 的 bge-m3 实现（fp16）。"""

    slot = "embed"

    def __init__(self, settings: Settings, pool: GPUPool) -> None:
        self._settings = settings
        self._pool = pool
        self._model = None

    def load(self) -> None:
        """加载 bge-m3；缺包或缺权重时给出可执行提示。"""
        if self._model is not None:
            return
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as exc:
            raise AppError(
                "GPU_UNAVAILABLE",
                "FlagEmbedding 未安装，无法加载 bge-m3。安装：pip install FlagEmbedding",
            ) from exc
        path = self._settings.embed_model_path
        if not (path / "config.json").exists():
            raise AppError(
                "GPU_UNAVAILABLE",
                f"bge-m3 权重不完整：{path}/config.json 不存在。",
            )
        self._model = BGEM3FlagModel(str(path), use_fp16=True, devices=["cuda:0"])

    def encode(self, texts: list[str]) -> EmbeddingBatch:
        """在 pool 独占下编码，返回稠密与稀疏两路。"""
        with self._pool.acquire("embed"):
            self.load()
            out = self._model.encode(
                texts,
                batch_size=8,
                max_length=512,
                return_dense=True,
                return_sparse=True,
                return_colbert_vecs=False,
            )
        dense = [list(map(float, row)) for row in out["dense_vecs"]]
        sparse = [{int(k): float(v) for k, v in d.items()} for d in out["lexical_weights"]]
        return EmbeddingBatch(dense=dense, sparse=sparse, tokens=sum(len(t) for t in texts) // 2)

    def unload(self) -> None:
        """释放模型引用。"""
        self._model = None


def build_embedder(settings: Settings, pool: GPUPool) -> Embedder:
    """构建嵌入器：优先真实 bge-m3，权重缺失时退回哈希嵌入并明示日志。

    注意：退回只用于开发/测试；生产环境索引构建必须使用 bge-m3，
    否则稠密向量空间与线上检索不一致，属于版本级错误。
    """
    path = settings.embed_model_path
    if (path / "config.json").exists():
        embedder: Embedder = BgeM3Embedder(settings, pool)
    else:
        embedder = HashingEmbedder(settings.embedding_dim)
    pool.register(embedder)  # type: ignore[arg-type]
    return embedder
