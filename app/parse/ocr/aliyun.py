"""阿里云 OCR 适配层（主供应商）。

只做一件事：把 SDK 返回的结构翻译成 OcrResult。
凭据、超时、重试与出域留痕由出域网关负责，本文件不读环境变量。
"""
from __future__ import annotations

import json
from io import BytesIO

from app.parse.ocr.base import OcrBlock, OcrProvider, OcrResult, ProviderError


class AliyunOcrProvider(OcrProvider):
    """阿里云通用文字识别（RecognizeGeneral）。"""

    name = "aliyun"
    endpoint = "ocr-api.cn-hangzhou.aliyuncs.com"

    def __init__(self, access_key_id: str, access_key_secret: str) -> None:
        self._ak = access_key_id
        self._sk = access_key_secret

    def recognize(self, image_bytes: bytes, *, timeout_s: int = 30) -> OcrResult:
        """调用阿里云 OCR；缺凭据或 SDK 异常统一抛 ProviderError，交给网关换供应商。"""
        if not self._ak or not self._sk:
            raise ProviderError(self.name, "credentials_missing")
        try:
            from alibabacloud_ocr_api20210707 import models as ocr_models
            from alibabacloud_ocr_api20210707.client import Client as OcrClient
            from alibabacloud_tea_openapi import models as open_api_models
            from alibabacloud_tea_util import models as util_models
        except ImportError as exc:
            raise ProviderError(self.name, "sdk_not_installed") from exc

        config = open_api_models.Config(
            access_key_id=self._ak,
            access_key_secret=self._sk,
            endpoint=self.endpoint,
        )
        client = OcrClient(config)
        request = ocr_models.RecognizeGeneralRequest(body=BytesIO(image_bytes))
        runtime = util_models.RuntimeOptions(
            read_timeout=timeout_s * 1000,
            connect_timeout=5000,
        )
        try:
            response = client.recognize_general_with_options(request, runtime)
        except Exception as exc:  # SDK 异常类型不稳定，统一翻译成供应商失败
            raise ProviderError(self.name, f"upstream_error: {exc}") from exc

        # Data 是一段 JSON 字符串，包含 content 与 prism_wordsInfo
        try:
            data = json.loads(response.body.data)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProviderError(self.name, "invalid_response") from exc

        words = data.get("prism_wordsInfo") or []
        blocks: list[OcrBlock] = []
        for item in words:
            text = (item.get("word") or "").strip()
            if not text:
                continue
            x = float(item.get("x", 0))
            y = float(item.get("y", 0))
            w = float(item.get("width", 0))
            h = float(item.get("height", 0))
            prob = item.get("prob")
            blocks.append(OcrBlock(
                text=text,
                bbox=(x, y, x + w, y + h),
                block_type="text",
                confidence=float(prob) if prob is not None else None,
            ))
        conf = [b.confidence for b in blocks if b.confidence is not None]
        return OcrResult(
            blocks=blocks,
            page_width=int(data.get("width", 0) or 0),
            page_height=int(data.get("height", 0) or 0),
            provider=self.name,
            provider_request_id=str(response.body.request_id or ""),
            confidence_avg=round(sum(conf) / len(conf), 4) if conf else 0.0,
        )
