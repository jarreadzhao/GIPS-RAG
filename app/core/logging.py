"""结构化 JSON 日志：按「接入 / 检索 / 生成 / 校验 / 台账」五分法分频道。

规则：
1. 一行一个 JSON 对象，便于 Prometheus/Langfuse 之外的落盘检索。
2. 频道（channel）是必填字段，用来回答「这条日志属于哪一段链路」。
3. 请求体默认不落日志（《契约》2.3），本模块也不提供记录请求体的入口。
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

# 五个法定频道，新增频道必须先改规格
CHANNELS = ("ingest", "retrieval", "generate", "verify", "ledger", "access")


class JsonFormatter(logging.Formatter):
    """把 LogRecord 渲染成单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "channel": getattr(record, "channel", "-"),
            "event": record.getMessage(),
        }
        # 业务字段统一从 extra 里的 fields 字典取，避免污染标准属性
        extra_fields = getattr(record, "fields", None)
        if isinstance(extra_fields, dict):
            payload.update(extra_fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """幂等地配置根日志器，只挂一个 stdout handler。"""
    root = logging.getLogger()
    if getattr(root, "_gips_configured", False):
        root.setLevel(level.upper())
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
    root.setLevel(level.upper())
    root._gips_configured = True  # type: ignore[attr-defined]
    # 降噪：uvicorn 的访问日志与业务 access 频道重复
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class ChannelLogger:
    """带固定频道的轻量日志门面，调用方不用每次传 channel。"""

    def __init__(self, channel: str) -> None:
        if channel not in CHANNELS:
            raise ValueError(f"未知日志频道 {channel}，允许值：{CHANNELS}")
        self.channel = channel
        self._logger = logging.getLogger(f"gips.{channel}")

    def info(self, event: str, **fields: Any) -> None:
        self._logger.info(event, extra={"channel": self.channel, "fields": fields})

    def warning(self, event: str, **fields: Any) -> None:
        self._logger.warning(event, extra={"channel": self.channel, "fields": fields})

    def error(self, event: str, **fields: Any) -> None:
        self._logger.error(event, extra={"channel": self.channel, "fields": fields})

    def exception(self, event: str, **fields: Any) -> None:
        self._logger.exception(event, extra={"channel": self.channel, "fields": fields})


def get_logger(channel: str) -> ChannelLogger:
    """按频道取日志器，频道名写错会在写第一行日志前就报错。"""
    return ChannelLogger(channel)
