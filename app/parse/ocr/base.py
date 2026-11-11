"""云 OCR 统一数据结构（CT-11 第 3 条：结果归一）。

下游只认 OcrResult，不允许出现 `if provider == 'aliyun'` 的分支散落在解析器里。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

BlockType = Literal["text", "table", "figure", "header", "footer"]


class ProviderError(RuntimeError):
    """供应商侧失败：由出域网关决定是否换第二家重试。"""

    def __init__(self, provider: str, reason: str) -> None:
        self.provider = provider
        self.reason = reason
        super().__init__(f"{provider}: {reason}")


@dataclass
class OcrBlock:
    """一个识别块。bbox 为像素坐标 [x0, y0, x1, y1]，左上原点。"""

    text: str
    bbox: tuple[float, float, float, float]
    block_type: BlockType = "text"
    confidence: float | None = None


@dataclass
class OcrResult:
    """两家供应商归一后的结果，字段固定，不允许增删。"""

    blocks: list[OcrBlock]
    page_width: int
    page_height: int
    provider: str
    provider_request_id: str
    confidence_avg: float
    elapsed_ms: int = 0

    def full_text(self) -> str:
        """按块顺序拼接全文，用于文本层替代物与锚点定位。"""
        return "\n".join(b.text for b in self.blocks if b.text.strip())

    def validate(self) -> None:
        """结构自检：缺字段即视为供应商返回不可用，不做静默容错。"""
        if not self.provider:
            raise ValueError("OcrResult 缺少 provider")
        if not self.provider_request_id:
            raise ValueError("OcrResult 缺少 provider_request_id")
        if self.page_width <= 0 or self.page_height <= 0:
            raise ValueError("OcrResult 页面尺寸非法")
        for b in self.blocks:
            if len(b.bbox) != 4:
                raise ValueError("OcrBlock.bbox 必须为 4 元组")


class OcrProvider(ABC):
    """云 OCR 供应商协议。"""

    name: str

    @abstractmethod
    def recognize(self, image_bytes: bytes, *, timeout_s: int = 30) -> OcrResult:
        """对裁边后的页面图像做识别。"""


class FakeOcrProvider(OcrProvider):
    """无凭据时的确定性替身：只用于验证出域三条禁令与状态机。"""

    def __init__(self, name: str = "fake", text: str = "[OCR-FAKE] 模拟识别文本") -> None:
        self.name = name
        self._text = text

    def recognize(self, image_bytes: bytes, *, timeout_s: int = 30) -> OcrResult:
        """不产生任何网络请求，直接返回固定结果。"""
        return OcrResult(
            blocks=[OcrBlock(text=self._text, bbox=(0, 0, 100, 20), confidence=0.99)],
            page_width=100,
            page_height=200,
            provider=self.name,
            provider_request_id="fake-request",
            confidence_avg=0.99,
            elapsed_ms=0,
        )
