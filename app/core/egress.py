"""出域网关（T-0-10 的落点，也是系统唯一出口）。

三条硬规则（CT-11）：
    1. 无审批即拒：B_internal 且 approval_ref 为空 -> OCR_APPROVAL_MISSING，不发请求。
    2. C_ledger 永久拒绝：OCR_FORBIDDEN_CORPUS，且不写任何出域记录。
    3. 结果归一：下游只认 OcrResult。

失败语义：第一家失败换第二家；两家都失败 -> 该页 ocr_failed，绝不静默跳过，
也绝不回退本地 OCR。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

from app.config import Settings
from app.core.db import Database
from app.core.logging import get_logger
from app.errors import AppError
from app.parse.ocr.aliyun import AliyunOcrProvider
from app.parse.ocr.base import OcrProvider, OcrResult, ProviderError
from app.parse.ocr.crop import CroppedImage
from app.parse.ocr.tencent import TencentOcrProvider


def _sha256(text: str) -> str:
    """返回 sha256 前缀 + 64 位小写十六进制，与契约 2.1 的 span_hash 格式一致。"""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class EgressGateway:
    """所有出域调用的唯一入口。"""

    def __init__(
        self,
        settings: Settings,
        db: Database | None = None,
        providers: dict[str, OcrProvider] | None = None,
    ) -> None:
        self._settings = settings
        self._db = db
        self._log = get_logger("ingest")
        self._cache: dict[str, OcrResult] = {}
        self._cache_lock = threading.Lock()
        self._providers = providers or self._build_providers()

    def _build_providers(self) -> dict[str, OcrProvider]:
        """按配置构建供应商表；凭据为空时对应 provider 会在调用时抛 credentials_missing。"""
        s = self._settings
        return {
            "aliyun": AliyunOcrProvider(s.aliyun_ak, s.aliyun_sk),
            "tencent": TencentOcrProvider(s.tencent_ak, s.tencent_sk),
        }

    # ---------- 出域主流程 ----------
    def ocr_page(
        self,
        *,
        doc_id: str,
        version_no: int,
        page_no: int,
        image: CroppedImage,
        corpus_type: str,
        approval_ref: str | None,
        idempotency_key: str | None = None,
    ) -> OcrResult:
        """把一页裁边图送去云 OCR，返回归一结果。"""
        # 规则 2：C 类台账永久禁止，连日志都不留（AC-12E 要求「无任何出域记录」）
        if corpus_type == "C_ledger":
            raise AppError(
                "OCR_FORBIDDEN_CORPUS",
                "C_ledger 台账页面永久禁止 OCR 出域，没有兜底路径，请转人工录入。",
                details={"doc_id": doc_id, "page_no": page_no},
            )

        key = idempotency_key or self._make_key(doc_id, version_no, page_no, image)
        with self._cache_lock:
            if key in self._cache:
                return self._cache[key]  # 同一幂等键不重复计费、不重复出域

        # 规则 1：B 类必须有已落档审批，禁止「先发后补」
        if corpus_type == "B_internal" and not approval_ref:
            self._write_log(
                doc_id=doc_id, version_no=version_no, page_no=page_no,
                endpoint="-", purpose="ocr", corpus_type=corpus_type,
                crop_box=image.box_pt, provider="-", state="blocked",
                payload_hash=_sha256(key), byte_size=0,
                dlp_verdict="approval_missing",
            )
            raise AppError(
                "OCR_APPROVAL_MISSING",
                "B_internal 页面缺少已落档的审批记录，拒绝出域。",
                details={"doc_id": doc_id, "page_no": page_no},
            )

        if not self._settings.egress_enabled:
            # 云通道关闭时不提供本地兜底，直接判失败（AC-12C）
            self._write_log(
                doc_id=doc_id, version_no=version_no, page_no=page_no,
                endpoint="-", purpose="ocr", corpus_type=corpus_type,
                crop_box=image.box_pt, provider="-", state="failed",
                payload_hash=_sha256(key), byte_size=len(image.png_bytes),
                dlp_verdict="egress_disabled",
            )
            raise AppError(
                "UPSTREAM_TIMEOUT",
                "云端通道已关闭，且本机不部署 OCR 模型；该页标 ocr_failed 转人工复核。",
                details={"doc_id": doc_id, "page_no": page_no},
            )

        last_error: Exception | None = None
        for name in self._settings.ocr_providers:
            provider = self._providers.get(name)
            if provider is None:
                continue
            started = time.perf_counter()
            try:
                result = provider.recognize(image.png_bytes,
                                             timeout_s=self._settings.ocr_timeout_s)
                # 腾讯云不返回页面尺寸，用裁边图尺寸回填，保证结构完整
                result.page_width = result.page_width or image.width_px
                result.page_height = result.page_height or image.height_px
                result.elapsed_ms = int((time.perf_counter() - started) * 1000)
                result.validate()
            except (ProviderError, ValueError) as exc:
                last_error = exc
                self._write_log(
                    doc_id=doc_id, version_no=version_no, page_no=page_no,
                    endpoint=name, purpose="ocr", corpus_type=corpus_type,
                    crop_box=image.box_pt, provider=name, state="failed",
                    payload_hash=_sha256(key), byte_size=len(image.png_bytes),
                    dlp_verdict=f"provider_error:{str(exc)[:80]}",
                )
                self._log.warning("ocr_provider_failed", provider=name,
                                  doc_id=doc_id, page_no=page_no, reason=str(exc))
                continue  # 换第二家供应商，不回退本地
            self._write_log(
                doc_id=doc_id, version_no=version_no, page_no=page_no,
                endpoint=name, purpose="ocr", corpus_type=corpus_type,
                crop_box=image.box_pt, provider=name, state="sent",
                payload_hash=_sha256(key), byte_size=len(image.png_bytes),
                dlp_verdict="pass",
            )
            with self._cache_lock:
                self._cache[key] = result
            return result

        raise AppError(
            "UPSTREAM_TIMEOUT",
            f"两家云 OCR 均失败（{last_error}），该页标 ocr_failed 并进入人工复核队列。",
            details={"doc_id": doc_id, "page_no": page_no},
        )

    def _make_key(self, doc_id: str, version_no: int, page_no: int,
                  image: CroppedImage) -> str:
        """幂等键 = sha256(doc_id+version_no+page_no+crop_box)，与裁边结果绑定。"""
        raw = f"{doc_id}{version_no}{page_no}{json.dumps(image.box_px, sort_keys=True)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # ---------- 留痕 ----------
    def _write_log(self, *, doc_id: str, version_no: int, page_no: int, endpoint: str,
                   purpose: str, corpus_type: str, crop_box: dict, provider: str,
                   state: str, payload_hash: str, byte_size: int,
                   dlp_verdict: str) -> None:
        """写一条出域记录；不存原始载荷，只存哈希与字节数。"""
        if self._db is None:
            return  # 无库场景（单测）只校验拦截逻辑

        def _insert(conn) -> None:
            prev = conn.execute(
                "SELECT self_hash FROM egress_logs ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
            prev_hash = prev["self_hash"] if prev else None
            created_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
            fields = {
                "doc_id": doc_id, "version_no": version_no, "page_no": page_no,
                "endpoint": endpoint, "purpose": purpose, "provider": provider,
                "state": state, "payload_hash": payload_hash,
            }
            # 规范化固定为字段名字典序 + = 连接（CT-8）
            normalized = "&".join(f"{k}={fields[k]}" for k in sorted(fields))
            self_hash = _sha256(f"{prev_hash or ''}{normalized}")
            conn.execute(
                """INSERT INTO egress_logs
                   (log_id, task_id, endpoint, purpose, egress_class, corpus_type,
                    doc_id, version_no, page_no, crop_box, provider, state,
                    payload_hash, byte_size, dlp_verdict, prev_hash, self_hash, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    _sha256(f"{payload_hash}{time.time_ns()}")[7:39],
                    None, endpoint, purpose, "page_image", corpus_type,
                    doc_id, version_no, page_no,
                    json.dumps(crop_box, sort_keys=True), provider, state,
                    payload_hash, byte_size, dlp_verdict, prev_hash, self_hash, created_at,
                ),
            )

        self._db.run_write(_insert)


def build_egress_gateway(settings: Settings, db: Database,
                         providers: dict[str, OcrProvider] | None = None) -> EgressGateway:
    """工厂：允许测试注入 FakeProvider，生产走真实 SDK 适配层。"""
    return EgressGateway(settings, db, providers)
