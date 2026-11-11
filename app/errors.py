"""统一错误结构：错误码只从《契约》2.2 的表里取，禁止各模块自造同义码。"""
from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

# 错误码 -> (HTTP 状态码, 是否可重试)，与《契约》2.2 保持一致
ERROR_CODES: dict[str, tuple[int, bool]] = {
    "ACL_SCOPE_MISSING": (400, False),
    "ACL_DENIED": (403, False),
    "PROFILE_MISSING": (400, False),
    "SUPPLY_NOT_ALLOWED": (422, False),
    "INDEX_NOT_ACTIVE": (409, True),
    "ANCHOR_INVALID": (422, False),
    "SPAN_MISMATCH": (422, True),
    "CALC_NOT_ALLOWED": (422, False),
    "SQL_GUARD_REJECTED": (400, False),
    "ROW_VERSION_CONFLICT": (409, True),
    "IDEMPOTENCY_CONFLICT": (409, False),
    "DOC_HASH_MISMATCH": (409, False),
    "UPSTREAM_TIMEOUT": (504, True),
    "UPSTREAM_RATE_LIMITED": (429, True),
    "CACHE_KEY_INCOMPLETE": (500, False),
    # 以下为《接口》3.5 与 CT-2/CT-8/CT-11 明确点名、但 2.2 表未收录的码，
    # 已在实施报告中登记为规格缺口，待变更单统一。
    "VALIDATION_FAILED": (400, False),
    "DOC_TOO_LARGE": (413, False),
    "MALWARE_DETECTED": (422, False),
    "CONFIRM_TOKEN_INVALID": (400, False),
    "OCR_APPROVAL_MISSING": (422, False),
    "OCR_FORBIDDEN_CORPUS": (422, False),
    "GPU_UNAVAILABLE": (503, True),
    "CONTEXT_BUDGET_EXCEEDED": (422, False),
    "DRAFT_NOT_SIGNABLE": (409, False),
}


class AppError(Exception):
    """业务异常：携带契约错误码，由全局处理器转成统一 JSON。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"未登记的错误码：{code}，请先补《契约》2.2")
        self.code = code
        self.message = message
        self.details = details or {}
        status, default_retryable = ERROR_CODES[code]
        self.http_status = status
        self.retryable = default_retryable if retryable is None else retryable
        super().__init__(message)


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """把 AppError 转成《接口》1.3 的统一错误结构。"""
    request_id = getattr(request.state, "request_id", "")
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "code": exc.code,
            "message": exc.message[:200],   # message 面向人，不得超 200 字
            "retryable": exc.retryable,
            "request_id": request_id,
            "details": exc.details or {},
        },
    )
