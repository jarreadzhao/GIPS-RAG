"""测试夹具：所有用例都在 tmp_path 下建库与对象目录，不碰仓库里的 var/。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.core.db import Database  # noqa: E402


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """把模型路径指向空目录，强制使用确定性替身，避免测试占用显存。"""
    return Settings(
        env="test",
        db_path=tmp_path / "gips.db",
        object_store_dir=tmp_path / "objects",
        embed_model_path=tmp_path / "no-embed",
        reranker_model_path=tmp_path / "no-rerank",
        gguf_path=tmp_path / "no.gguf",
        llm_backend="fake",
        egress_enabled=True,
        aliyun_ak="",
        aliyun_sk="",
        tencent_ak="",
        tencent_sk="",
    )


@pytest.fixture()
def db(settings: Settings) -> Database:
    """建好表与触发器的临时库。"""
    database = Database(settings.db_path)
    database.init_schema()
    return database
