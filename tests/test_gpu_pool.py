"""显存池：同一时刻只允许一个模型常驻（AC-12A 的单元级验证）。"""
from __future__ import annotations

from app.core.gpu_pool import GPUPool


class FakeModel:
    """记录加载/卸载次数的模型替身。"""

    def __init__(self, slot: str) -> None:
        self.slot = slot
        self.loaded = 0
        self.unloaded = 0
        self.is_loaded = False

    def load(self) -> None:
        self.loaded += 1
        self.is_loaded = True

    def unload(self) -> None:
        self.unloaded += 1
        self.is_loaded = False


def _pool() -> GPUPool:
    # 固定读数，避免依赖机器上是否装了 nvidia-smi
    return GPUPool(vram_reader=lambda: 1000, empty_cache=lambda: None,
                   release_wait_s=0.1, release_poll_s=0.01)


def test_switching_unloads_previous_model():
    """切到重排时必须先卸载嵌入，常驻集合始终 ≤1。"""
    pool = _pool()
    embed = FakeModel("embed")
    rerank = FakeModel("rerank")
    pool.register(embed)
    pool.register(rerank)

    with pool.acquire("embed"):
        assert pool.resident() == ["embed"]
    with pool.acquire("rerank"):
        assert pool.resident() == ["rerank"]
        assert embed.is_loaded is False
        assert embed.unloaded == 1

    assert pool.resident() == ["rerank"]


def test_same_slot_is_reused_without_reload():
    """同一槽位连续 acquire 不重复加载。"""
    pool = _pool()
    embed = FakeModel("embed")
    pool.register(embed)
    for _ in range(3):
        with pool.acquire("embed"):
            pass
    assert embed.loaded == 1


def test_release_returns_memory():
    """release 后常驻集合为空。"""
    pool = _pool()
    pool.register(FakeModel("generate"))
    with pool.acquire("generate"):
        pass
    pool.release("generate")
    assert pool.resident() == []


def test_co_resident_embed_and_rerank_stay_loaded():
    """检索场景：嵌入与重排共存，生成任务仍会清空它们。"""
    pool = GPUPool(vram_reader=lambda: 1000, empty_cache=lambda: None,
                   release_wait_s=0.1, release_poll_s=0.01,
                   co_resident_slots=("embed", "rerank"))
    embed = FakeModel("embed")
    rerank = FakeModel("rerank")
    generate = FakeModel("generate")
    for model in (embed, rerank, generate):
        pool.register(model)

    with pool.acquire("embed"):
        pass
    with pool.acquire("rerank"):
        assert pool.resident() == ["embed", "rerank"]
        assert embed.is_loaded is True
    # 第二次检索复用两个模型，不再触发加载
    with pool.acquire("embed"):
        pass
    assert embed.loaded == 1 and rerank.loaded == 1

    with pool.acquire("generate"):
        assert pool.resident() == ["generate"]
        assert embed.unloaded == 1 and rerank.unloaded == 1
