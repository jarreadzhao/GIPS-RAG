"""生成模型适配层（T-0-04）。

抽象成 LLMBackend 的原因：当前选 llama.cpp 进程内推理，但接口不能绑定实现，
将来换 vLLM 或 llama-server 只替换实现，不动推理链。

三条硬约束写死在参数上：
    1. n_ctx 默认 4096（8GB 卡上限），超过配置层就拒绝启动。
    2. tokens_in/out 必须回传，用于配额对账。
    3. 上下文超预算时报 CONTEXT_BUDGET_EXCEEDED，绝不截断。
"""
from __future__ import annotations

import hashlib
import math
import os
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.config import Settings
from app.core.gpu_pool import GPUPool
from app.errors import AppError

# llama.cpp 的 CUDA 轮子只带 llama.dll，依赖的 cudart / cublas 由 torch 提供。
# Windows 加载 DLL 不会去翻 site-packages，必须显式把 torch\lib 加进搜索路径，
# 否则 import 时报「Could not find module llama.dll（或它的某个依赖）」。
_DLL_HANDLES: list = []


def _prepare_cuda_dll_path() -> None:
    if sys.platform != "win32" or _DLL_HANDLES:
        return
    try:
        import torch
    except ImportError:
        return
    lib_dir = os.path.join(os.path.dirname(torch.__file__), "lib")
    if not os.path.isdir(lib_dir):
        return
    os.environ["PATH"] = lib_dir + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        # 返回的句柄必须留住，释放后 DLL 搜索路径随之失效
        _DLL_HANDLES.append(os.add_dll_directory(lib_dir))


@dataclass
class LLMRequest:
    """一次生成请求。prompt 已由提示词层拼好，本层不做任何拼装。"""

    prompt: str
    max_tokens: int = 600
    temperature: float = 0.0          # 审查链要求可复现，默认贪心
    stop: list[str] = field(default_factory=list)


@dataclass
class LLMResult:
    """一次生成结果。"""

    text: str
    tokens_in: int
    tokens_out: int
    model_fingerprint: str
    payload: dict | None = None       # 结构化输出（若后端要求 JSON）


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：英文约 4 字符/token，中文约 1.5 字符/token。

    只用于预算守卫与 fake 后端的计数，真实后端以 usage 字段为准。
    """
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    other_chars = len(text) - ascii_chars
    return max(1, math.ceil(ascii_chars / 4 + other_chars / 1.5))


class LLMBackend(ABC):
    """生成后端协议。"""

    slot = "generate"

    @abstractmethod
    def generate(self, req: LLMRequest) -> LLMResult:
        """执行一次生成。"""

    @abstractmethod
    def unload(self) -> None:
        """释放权重。"""


class FakeLLM(LLMBackend):
    """无 GGUF 时的确定性替身。

    用途：让 T-0-01…T-0-12 的接口、预算守卫与显存池可以独立验证；
    它不产生任何业务判定，输出内容固定，避免被误当成模型能力。
    """

    slot = "generate"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._ctx_limit = settings.llm_n_ctx
        self._fingerprint = "fake-llm-0.1"

    def load(self) -> None:
        """替身没有权重，加载是空操作。"""

    def generate(self, req: LLMRequest) -> LLMResult:
        tokens_in = estimate_tokens(req.prompt)
        # 预算守卫：超限必须报错，不截断（AC-47A）
        budget = int(self._ctx_limit * self._settings.llm_ctx_budget_ratio)
        if tokens_in > budget:
            raise AppError(
                "CONTEXT_BUDGET_EXCEEDED",
                f"输入 token {tokens_in} 超过预算 {budget}（ctx_limit={self._ctx_limit} 的 85%），"
                "必须分段，不得截断。",
                details={"tokens_in": tokens_in, "budget": budget},
            )
        text = "[FAKE-LLM] " + req.prompt[:40]
        return LLMResult(
            text=text,
            tokens_in=tokens_in,
            tokens_out=estimate_tokens(text),
            model_fingerprint=self._fingerprint,
        )

    def unload(self) -> None:
        """替身没有权重，卸载是空操作。"""


class LlamaCppBackend(LLMBackend):
    """llama.cpp 进程内实现（Qwen3-8B GGUF Q4_K_M）。

    显存由 GPUPool 统一管理：本类只在 pool.acquire('generate') 内被调用，
    不自行卸载其他模型（CT-2 显存协作规则）。
    """

    slot = "generate"

    def __init__(self, settings: Settings, pool: GPUPool) -> None:
        self._settings = settings
        self._pool = pool
        self._llm = None
        self._fingerprint: str | None = None

    def load(self) -> None:
        """加载 GGUF 到显存；缺包或缺权重时给出可执行的错误提示。"""
        if self._llm is not None:
            return
        try:
            _prepare_cuda_dll_path()
            from llama_cpp import Llama  # 延迟导入：无 CUDA wheel 时不影响其他模块
        except ImportError as exc:
            raise AppError(
                "GPU_UNAVAILABLE",
                "llama-cpp-python 未安装或缺少 CUDA 后端。请按 scripts/install_llama_cpp.ps1 "
                "安装 cu124 wheel 后把 GIPS_LLM_BACKEND 设为 llama_cpp。",
            ) from exc
        gguf = self._settings.gguf_path
        if not gguf.exists():
            raise AppError(
                "GPU_UNAVAILABLE",
                f"未找到 GGUF 权重 {gguf}。下载 Qwen3-8B-Instruct Q4_K_M 后重试。",
            )
        self._llm = Llama(
            model_path=str(gguf),
            n_ctx=self._settings.llm_n_ctx,
            n_gpu_layers=self._settings.llm_n_gpu_layers,
            n_batch=self._settings.llm_n_batch,
            verbose=False,
        )
        # 指纹用权重文件大小 + 路径哈希，保证同一权重得到同一标识
        digest = hashlib.sha256(f"{gguf.stat().st_size}:{gguf}".encode()).hexdigest()[:16]
        self._fingerprint = f"llamacpp-{digest}"

    def generate(self, req: LLMRequest) -> LLMResult:
        """按 pool 独占方式调用一次 chat completion。"""
        tokens_in = estimate_tokens(req.prompt)
        budget = int(self._settings.llm_n_ctx * self._settings.llm_ctx_budget_ratio)
        if tokens_in > budget:
            raise AppError(
                "CONTEXT_BUDGET_EXCEEDED",
                f"输入 token {tokens_in} 超过预算 {budget}，必须分段，不得截断。",
                details={"tokens_in": tokens_in, "budget": budget},
            )
        with self._pool.acquire("generate", ctx_tokens=tokens_in,
                                batch_size=self._settings.llm_n_batch):
            # 首次 acquire 会触发 load()，因此模型就绪检查必须放在 acquire 之后
            if self._llm is None:
                raise AppError("GPU_UNAVAILABLE", "生成模型加载失败")
            resp = self._llm.create_chat_completion(
                messages=[{"role": "user", "content": self._prompt_for_model(req.prompt)}],
                max_tokens=req.max_tokens,
                temperature=req.temperature,
                stop=req.stop or None,
            )
        usage = resp.get("usage", {})
        text = resp["choices"][0]["message"]["content"]
        return LLMResult(
            text=text,
            tokens_in=int(usage.get("prompt_tokens", tokens_in)),
            tokens_out=int(usage.get("completion_tokens", estimate_tokens(text))),
            model_fingerprint=self._fingerprint or "llamacpp",
        )

    def _prompt_for_model(self, prompt: str) -> str:
        """Qwen3 默认先输出思考过程，会把 JSON 挤掉。

        llama-cpp-python 当前版本不支持 chat_template_kwargs，只能用 Qwen3
        官方的 `/no_think` 软开关；其他模型原样透传。
        """
        if "qwen3" in self._settings.gguf_path.name.lower():
            return prompt + "\n/no_think"
        return prompt

    def unload(self) -> None:
        """释放 llama.cpp 对象；真正归还显存依赖析构 + gc。"""
        self._llm = None


def build_llm_backend(settings: Settings, pool: GPUPool) -> LLMBackend:
    """按配置构建后端并注册到显存池。"""
    backend: LLMBackend
    if settings.llm_backend == "llama_cpp":
        backend = LlamaCppBackend(settings, pool)
    else:
        backend = FakeLLM(settings)
    pool.register(backend)  # type: ignore[arg-type]
    return backend
