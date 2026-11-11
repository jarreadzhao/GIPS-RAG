"""AC-4：固定长度 + 重叠的切分配置必须让服务拒绝启动。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.parse.chunker import validate_chunk_config


def test_overlap_rejected(settings):
    """重叠 200 字符即校验失败，并指明禁用策略。"""
    with pytest.raises(ValidationError) as exc:
        Settings(chunker_overlap=200, db_path=settings.db_path)
    assert "禁止" in str(exc.value)
    assert "重叠" in str(exc.value)


def test_fixed_length_strategy_rejected(settings):
    """fixed_length 策略本身被禁用。"""
    with pytest.raises(ValidationError) as exc:
        Settings(chunker_strategy="fixed_length", db_path=settings.db_path)
    assert "fixed_length" in str(exc.value)


def test_default_config_valid(settings):
    """默认配置（provision + overlap=0）应通过。"""
    assert settings.chunker_overlap == 0
    assert settings.chunker_strategy == "provision"


def test_chunker_double_checks():
    """绕过配置直接调切分器也要被拦下。"""
    with pytest.raises(ValueError):
        validate_chunk_config("paragraph", 50)
    with pytest.raises(ValueError):
        validate_chunk_config("fixed_length", 0)
    validate_chunk_config("paragraph", 0)
