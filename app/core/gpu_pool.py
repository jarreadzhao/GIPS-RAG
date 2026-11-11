"""显存分时独占管理器（T-0-03）。

为什么必须先有这个模块：
    RTX 4060 Laptop 只有 8GB，嵌入(≈1.2G) + 重排(≈1.2G) + 生成(≈5G) 三者
    合计超过上限，任何「三个都加载好常驻」的设计都会 OOM。因此本进程内所有
    模型加载/卸载必须经过同一个闸门，且同一时刻只允许一个槽位常驻。

对外契约：
    pool.acquire("generate")  ->  上下文管理器，进入前卸载其他模型并加载目标
    pool.resident()           ->  当前常驻槽位列表（用于 OOM 归因与健康检查）
    pool.queue_position()     ->  当前排队位次（并发为 1 的排队语义）
"""
from __future__ import annotations

import gc
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator, Protocol

from app.core.logging import get_logger
from app.errors import AppError


class GPUModel(Protocol):
    """可被池管理的模型适配器。"""

    slot: str

    def load(self) -> None:
        """加载权重到显存。"""

    def unload(self) -> None:
        """释放权重并归还显存。"""


@dataclass
class ResidentEntry:
    """常驻记录，供 OOM 归因使用。"""

    slot: str
    model: GPUModel
    loaded_at: float
    vram_mb: int | None = None


@dataclass
class OOMSnapshot:
    """OOM 发生前的现场，缺任一项视为证据不足（CT-6 责任划分）。"""

    resident_models: list[str] = field(default_factory=list)
    ctx_tokens: int | None = None
    batch_size: int | None = None
    stage: str = "-"


def nvidia_used_mb() -> int | None:
    """读取整卡已用显存（MB）；无 NVIDIA 驱动或命令失败时返回 None。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        first = out.stdout.strip().splitlines()[0]
        return int(first.strip())
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def torch_empty_cache() -> None:
    """清空 PyTorch 的 CUDA 缓存；未安装 CUDA 版 torch 时静默跳过。"""
    try:
        import torch  # 延迟导入，避免无 GPU 环境下启动失败

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception:  # pragma: no cover - 取决于运行环境
        return


class GPUPool:
    """单进程显存闸门。

    线程模型：一把可重入锁 + 一个「独占」计数。acquire 期间其他线程阻塞等待，
    等待者数量即 queue_position 的来源。
    """

    def __init__(
        self,
        *,
        vram_reader: Callable[[], int | None] = nvidia_used_mb,
        empty_cache: Callable[[], None] = torch_empty_cache,
        release_wait_s: float = 15.0,
        release_poll_s: float = 0.3,
        release_tolerance_mb: int = 512,
        co_resident_slots: tuple[str, ...] = (),
    ) -> None:
        self._lock = threading.RLock()
        self._models: dict[str, GPUModel] = {}          # 已注册的适配器
        self._resident: dict[str, ResidentEntry] = {}   # 当前常驻槽位
        self._waiting = 0                                # 排队线程数
        # 排队计数用独立小锁：_lock 在模型调用全程被持有，
        # 若读取位次也走 _lock，观测方会一直阻塞到本次 GPU 操作结束（AC-71A 要）
        self._waiting_lock = threading.Lock()
        self._vram_reader = vram_reader
        self._empty_cache = empty_cache
        self._release_wait_s = release_wait_s
        self._release_poll_s = release_poll_s
        self._release_tolerance_mb = release_tolerance_mb
        # 允许共存的槽位：检索需要在一次请求里先嵌入再重排，
        # 若强制互斥，每次请求都要卸载/重载另一个模型，800ms 通道预算必然爆掉。
        # 默认空元组保持 M0 的「单槽位独占」语义。
        self._co_resident_slots = tuple(co_resident_slots)
        self._last_switch_ms: float | None = None        # 最近一次切换耗时
        self._oom_snapshots: list[OOMSnapshot] = []      # OOM 现场保留，便于归因
        self._log = get_logger("generate")

    # ---------- 注册 ----------
    def register(self, model: GPUModel) -> None:
        """注册模型适配器；同一槽位只允许一个实现。"""
        with self._lock:
            if model.slot in self._models and self._models[model.slot] is not model:
                raise RuntimeError(f"槽位 {model.slot} 已注册其他实现")
            self._models[model.slot] = model

    # ---------- 查询 ----------
    def resident(self) -> list[str]:
        """返回当前常驻槽位列表（AC-12A 用它与 nvidia-smi 交叉验证）。"""
        with self._lock:
            return sorted(self._resident.keys())

    def queue_position(self) -> int:
        """返回排队位次：0 表示无人排队，1 表示下一个。

        只统计「尚未拿到闸门」的线程；用独立小锁读取，
        不会因为模型正在跑而被阻塞到本次 GPU 操作结束。
        """
        with self._waiting_lock:
            return self._waiting

    def last_switch_ms(self) -> float | None:
        """返回最近一次模型切换耗时（毫秒）。"""
        return self._last_switch_ms

    def vram_usage_mb(self) -> tuple[int | None, int | None]:
        """返回 (已用, 总量) 显存 MB，读不到时返回 (None, None)。"""
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5, check=True,
            )
            used, total = out.stdout.strip().splitlines()[0].split(",")
            return int(used.strip()), int(total.strip())
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return None, None

    def oom_snapshots(self) -> list[OOMSnapshot]:
        """返回历史 OOM 现场，供 /metrics 与排障使用。"""
        return list(self._oom_snapshots)

    # ---------- 核心 ----------
    @contextmanager
    def acquire(self, slot: str, *, ctx_tokens: int | None = None,
                batch_size: int | None = None) -> Iterator[GPUModel]:
        """独占指定槽位，退出时归还占用（但不卸载模型）。"""
        model = self._models.get(slot)
        if model is None:
            raise AppError("GPU_UNAVAILABLE", f"槽位 {slot} 未注册模型适配器")

        # 先计数再抢锁：位次必须在真正拿到闸门之前就可被观测，
        # 否则等待中的线程永远看不到自己排在第几（AC-71A）。
        with self._waiting_lock:
            self._waiting += 1
        entered = False
        try:
            with self._lock:
                # 已经拿到闸门，不再算排队；进入后立即自减保证位次随进度递减
                with self._waiting_lock:
                    self._waiting -= 1
                    entered = True
                started = time.perf_counter()
                self._switch_to(slot, ctx_tokens=ctx_tokens, batch_size=batch_size)
                self._last_switch_ms = (time.perf_counter() - started) * 1000

                try:
                    yield model
                except RuntimeError as exc:
                    if "out of memory" in str(exc).lower():
                        # 记录现场后再抛，否则只剩一句 OOM，无法归因
                        self._oom_snapshots.append(OOMSnapshot(
                            resident_models=self.resident(),
                            ctx_tokens=ctx_tokens,
                            batch_size=batch_size,
                            stage=slot,
                        ))
                        self._log.error(
                            "gpu_oom", slot=slot, resident=self.resident(),
                            ctx_tokens=ctx_tokens, batch_size=batch_size,
                        )
                        raise AppError(
                            "GPU_UNAVAILABLE",
                            "显存不足（OOM）。已记录常驻模型、上下文与批大小，按 P0 缺陷处理。",
                            details={"resident_models": self.resident(),
                                     "ctx_tokens": ctx_tokens,
                                     "batch_size": batch_size},
                        ) from exc
                    raise
        finally:
            if not entered:
                # 抢锁期间被取消/异常退出，计数必须归还，否则位次会永久偏高
                with self._waiting_lock:
                    self._waiting -= 1

    def _switch_to(self, slot: str, *, ctx_tokens: int | None, batch_size: int | None) -> None:
        """按共存策略卸载非目标槽位，再加载目标槽位。调用方必须持有锁。"""
        if slot in self._resident:
            return  # 已常驻，直接复用

        # 目标与当前常驻都属共存集合时保留现有模型；否则先清空
        keep = (bool(self._co_resident_slots)
                and slot in self._co_resident_slots
                and all(other in self._co_resident_slots for other in self._resident))
        if not keep:
            baseline = self._vram_reader()
            had_resident = bool(self._resident)
            for other in list(self._resident.keys()):
                self._release(other)
            if had_resident and baseline is not None:
                # 卸载后必须确认显存真的归还，否则下一次加载会叠加上去
                self._wait_release(baseline)
        target = self._models[slot]
        try:
            target.load()
        except RuntimeError as exc:
            if "out of memory" in str(exc).lower():
                self._oom_snapshots.append(OOMSnapshot(
                    resident_models=self.resident(), ctx_tokens=ctx_tokens,
                    batch_size=batch_size, stage=f"load:{slot}",
                ))
                raise AppError("GPU_UNAVAILABLE", f"加载 {slot} 时显存不足") from exc
            raise
        used = self._vram_reader()
        self._resident[slot] = ResidentEntry(
            slot=slot, model=target, loaded_at=time.time(), vram_mb=used,
        )
        self._log.info("gpu_model_loaded", slot=slot, resident=self.resident(),
                       used_mb=used, co_resident=keep)

    def _release(self, slot: str) -> None:
        """卸载指定槽位并尽力归还显存。"""
        entry = self._resident.pop(slot, None)
        if entry is None:
            return
        try:
            entry.model.unload()
        finally:
            # 引用计数归零后 Python 才会真正析构权重，必须显式 gc
            entry.model = None  # type: ignore[assignment]
            gc.collect()
            self._empty_cache()
        self._log.info("gpu_model_unloaded", slot=slot, resident=self.resident())

    def _wait_release(self, baseline_mb: int) -> None:
        """轮询等待显存回落到基线附近，超时不阻塞业务（记警告）。"""
        deadline = time.time() + self._release_wait_s
        while time.time() < deadline:
            used = self._vram_reader()
            if used is None:
                return
            if used <= baseline_mb + self._release_tolerance_mb:
                return
            time.sleep(self._release_poll_s)
        self._log.warning("gpu_release_slow", waited_s=self._release_wait_s,
                          baseline_mb=baseline_mb, used_mb=self._vram_reader())

    def release(self, slot: str) -> None:
        """公开的卸载入口：显存诊断脚本与优雅退出使用。"""
        with self._lock:
            self._release(slot)

    def shutdown(self) -> None:
        """进程退出时卸载全部模型，保证显存干净退出。"""
        with self._lock:
            for slot in list(self._resident.keys()):
                self._release(slot)
