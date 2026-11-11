"""AC-71A：`GET /reviews/{task_id}` 必须暴露可观测的排队位次。

压测脚本靠这个字段采集 queue_position 序列，验证排队随进度递减；
字段缺失时压测拿不到排队证据，AC-71A 无法判定。
"""
from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from app.core.gpu_pool import GPUPool
from app.main import build_context, create_app


def _seed_task(db) -> str:
    """造一条 QA 任务行；状态查询只依赖 review_tasks，不需要真实检索。"""
    task_id = "tsk_queue_observability"
    db.run_write(lambda conn: conn.execute(
        "INSERT INTO review_tasks (task_id, task_type, supply, inference_scope, "
        "scope_json, status, queued_at, started_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, "qa", "self_hosted", "local_single_gpu", "{}", "succeeded",
         "2026-11-10T00:00:00Z", "2026-11-10T00:00:00Z",
         "2026-11-10T00:00:01Z")))
    return task_id


def test_review_status_exposes_queue_position(db, settings):
    """①有显存池时 queue_position 是整数（空闲为 0）。"""
    task_id = _seed_task(db)
    ctx = build_context(settings)
    app = create_app(ctx)
    with TestClient(app) as client:
        resp = client.get(f"/reviews/{task_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert "queue_position" in body
    assert isinstance(body["queue_position"], int)
    assert body["queue_position"] == 0          # 无请求在跑时无人排队


def test_review_status_queue_position_none_without_pool(db, settings):
    """②池缺失时必须返回 null 而不是抛错（兼容精简 context）。"""
    task_id = _seed_task(db)
    ctx = build_context(settings)
    ctx.pool = None
    app = create_app(ctx)
    # 不用 with：lifespan 退出会调 pool.shutdown()，本用例刻意把池置空
    client = TestClient(app)
    resp = client.get(f"/reviews/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["queue_position"] is None


class _DummyModel:
    """不占显存的槽位替身，只为验证排队计数。"""

    slot = "unit"

    def load(self) -> None:
        pass

    def unload(self) -> None:
        pass


def test_queue_position_counts_waiters_without_blocking():
    """③排队位次可被外部读到，且不随 GPU 多久而阻塞（旧实现会卡死）。"""
    pool = GPUPool(vram_reader=lambda: None, empty_cache=lambda: None)
    pool.register(_DummyModel())
    holding, release = threading.Event(), threading.Event()

    def hold() -> None:
        with pool.acquire("unit"):
            holding.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(2)
    assert pool.queue_position() == 0        # 只有执行者，无人排队

    def wait_one() -> None:
        with pool.acquire("unit"):
            pass

    waiter = threading.Thread(target=wait_one)
    waiter.start()
    deadline = time.time() + 2
    while time.time() < deadline and pool.queue_position() < 1:
        time.sleep(0.01)
    assert pool.queue_position() == 1        # 第二个线程排在第一位

    release.set()
    holder.join(5)
    waiter.join(5)
    assert pool.queue_position() == 0
