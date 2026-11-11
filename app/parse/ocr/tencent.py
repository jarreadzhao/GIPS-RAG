"""腾讯云 OCR 适配层（兜底供应商）。

兜底的含义是「换一家云供应商重试」，绝不回退到本地 OCR——
本地 OCR 常驻约 2GB，会挤掉生成模型的显存窗口（PRD 4.2）。
"""
from __future__ import annotations

import base64

from app.parse.ocr.base import OcrBlock, OcrProvider, OcrResult, ProviderError


class TencentOcrProvider(OcrProvider):
    """腾讯云通用印刷体识别（GeneralBasicOCR）。"""

    name = "tencent"

    def __init__(self, secret_id: str, secret_key: str, region: str = "ap-guangzhou") -> None:
        self._sid = secret_id
        self._skey = secret_key
        self._region = region

    def recognize(self, image_bytes: bytes, *, timeout_s: int = 30) -> OcrResult:
        """调用腾讯云 OCR，字段归一为 OcrResult。"""
        if not self._sid or not self._skey:
            raise ProviderError(self.name, "credentials_missing")
        try:
            from tencentcloud.common import credential
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
            from tencentcloud.ocr.v20181119 import models as ocr_models
            from tencentcloud.ocr.v20181119 import ocr_client
        except ImportError as exc:
            raise ProviderError(self.name, "sdk_not_installed") from exc

        cred = credential.Credential(self._sid, self._skey)
        http_profile = HttpProfile(endpoint="ocr.tencentcloudapi.com",
                                   reqTimeout=timeout_s)
        client_profile = ClientProfile(httpProfile=http_profile)
        client = ocr_client.OcrClient(cred, self._region, client_profile)

        request = ocr_models.GeneralBasicOCRRequest()
        request.ImageBase64 = base64.b64encode(image_bytes).decode()
        try:
            response = client.GeneralBasicOCR(request)
        except Exception as exc:  # SDK 异常类型不稳定，统一翻译
            raise ProviderError(self.name, f"upstream_error: {exc}") from exc

        payload = response.to_dict() if hasattr(response, "to_dict") else response.__dict__
        detections = payload.get("TextDetections") or []
        blocks: list[OcrBlock] = []
        for item in detections:
            text = (item.get("DetectedText") or "").strip()
            if not text:
                continue
            polygon = item.get("Polygon") or []
            if len(polygon) >= 8:
                xs = [float(polygon[i]) for i in range(0, 8, 2)]
                ys = [float(polygon[i]) for i in range(1, 8, 2)]
                bbox = (min(xs), min(ys), max(xs), max(ys))
            else:
                bbox = (0.0, 0.0, 0.0, 0.0)
            conf = item.get("Confidence")
            blocks.append(OcrBlock(
                text=text,
                bbox=bbox,
                block_type="text",
                confidence=float(conf) / 100.0 if conf is not None else None,
            ))
        confs = [b.confidence for b in blocks if b.confidence is not None]
        request_id = payload.get("RequestId", "")
        return OcrResult(
            blocks=blocks,
            page_width=0,          # 腾讯云不返回页面尺寸，由网关按上传图尺寸回填
            page_height=0,
            provider=self.name,
            provider_request_id=str(request_id),
            confidence_avg=round(sum(confs) / len(confs), 4) if confs else 0.0,
        )
