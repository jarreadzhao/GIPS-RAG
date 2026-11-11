"""重排模型适配层（T-0-05）。

bge-reranker-v2-m3 只做「查询-片段」打分，不做生成。
一期重排超时 1s 即降级为 RRF 原序（契约 2.2），本层负责把异常翻译成可判断的形态。
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod

from app.config import Settings
from app.core.gpu_pool import GPUPool
from app.errors import AppError

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


class Reranker(ABC):
    """重排后端协议。"""

    slot = "rerank"

    @abstractmethod
    def score(self, query: str, passages: list[str]) -> list[float]:
        """返回与 passages 等长的相关性分数（越大越相关）。"""

    @abstractmethod
    def unload(self) -> None:
        """释放权重。"""


class LexicalReranker(Reranker):
    """词面重合打分，用于无 GPU 的测试；不参与任何指标宣称。"""

    slot = "rerank"

    def load(self) -> None:
        """无权重。"""

    def score(self, query: str, passages: list[str]) -> list[float]:
        q = set(_TOKEN_RE.findall(query.lower()))
        out: list[float] = []
        for passage in passages:
            p = set(_TOKEN_RE.findall(passage.lower()))
            inter = len(q & p)
            union = len(q | p) or 1
            out.append(inter / union)
        return out

    def unload(self) -> None:
        """无权重。"""


class BgeReranker(Reranker):
    """FlagEmbedding 的 bge-reranker-v2-m3 实现（fp16）。"""

    slot = "rerank"

    def __init__(self, settings: Settings, pool: GPUPool) -> None:
        self._settings = settings
        self._pool = pool
        self._model = None

    def load(self) -> None:
        """加载重排权重。"""
        if self._model is not None:
            return
        try:
            from FlagEmbedding import FlagReranker
        except ImportError as exc:
            raise AppError(
                "GPU_UNAVAILABLE",
                "FlagEmbedding 未安装，无法加载 bge-reranker-v2-m3。",
            ) from exc
        path = self._settings.reranker_model_path
        if not (path / "config.json").exists():
            raise AppError("GPU_UNAVAILABLE", f"重排权重不完整：{path}/config.json 不存在。")
        self._model = FlagReranker(str(path), use_fp16=True, devices=["cuda:0"])

    def score(self, query: str, passages: list[str]) -> list[float]:
        """在 pool 独占下打分。"""
        if not passages:
            return []
        with self._pool.acquire("rerank"):
            self.load()
            pairs = [[query, p] for p in passages]
            scores = self._model.compute_score(pairs, normalize=True)
        if isinstance(scores, (int, float)):
            return [float(scores)]
        return [float(s) for s in scores]

    def unload(self) -> None:
        """释放模型引用。"""
        self._model = None


def build_reranker(settings: Settings, pool: GPUPool) -> Reranker:
    """构建重排器：权重存在则用真实模型，否则退回词面打分并保留明确标识。"""
    path = settings.reranker_model_path
    reranker: Reranker
    if (path / "config.json").exists():
        reranker = BgeReranker(settings, pool)
    else:
        reranker = LexicalReranker()
    pool.register(reranker)  # type: ignore[arg-type]
    return reranker
