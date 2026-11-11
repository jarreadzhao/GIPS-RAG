"""QA 链路压测（AC-71 / AC-71A / AC-71B）。

测量条件（写进报告必须带上）：
    本机 127.0.0.1:8011，embedder=bge-m3、reranker=bge-reranker-v2-m3，
    QA 走抽取式答案（`_extractive_answer`），rounds=0，不调生成模型。
    因此这里测的是「嵌入 + 重排 + 引用回库回验」的时延，不是生成时延。

两种时延的取法：
    端到端（e2e）：客户端实测，包含排队。
    执行（exec） ：review_tasks 的 started_at → finished_at。
                  注意 started_at 由编排器在「任务创建时」写入（检索之前），
                  所以这个窗口**包含** GPU 排队等待，不是「不含排队」的执行时延；
                  两者之差（queue_wait_p95）只反映客户端开销，
                  真实排队看 queue_series。
                  两列由 time.gmtime() 写入，精度只有 1 秒，exec 为整秒值
                  （可能为 0），见 JSON 的 exec_resolution_s。
                  AC-71 阈值 12s，1 秒量化不改变通过/不通过结论。
"""
from __future__ import annotations

import argparse
import asyncio
import calendar
import json
import math
import random
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

# 真实条款问题，轮转使用；同一批里尽量覆盖不同条款与提问方式
QUERIES = [
    "portfolio company 的定义在哪个条款",
    "1.A.4 要求什么",
    "carve-out 的披露要求",
    "GIPS 对组合收益披露有什么要求",
    "机构必须记录哪些政策和程序",
    "composite 的定义在哪个条款",
    "1.A.5 要求机构做什么",
    "record keeping 的要求在哪一条",
]
# 写进 JSON，报告里据此说明测的是什么
MEASURE_CONDITION = ("127.0.0.1:8011, bge-m3 + bge-reranker-v2-m3, "
                     "QA 抽取式链路（无生成模型，rounds=0）")
HEADERS = {"X-Role": "reviewer", "X-Dept": "compliance"}


def percentile(values: list[float], p: float) -> float | None:
    """线性插值分位数（numpy 默认口径），空输入返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * p / 100.0
    low, high = math.floor(pos), math.ceil(pos)
    if low == high:
        return ordered[int(pos)]
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _parse_utc(text: str | None) -> float | None:
    """解析 review_tasks 里的 'YYYY-MM-DDTHH:MM:SSZ' 为时间戳。"""
    if not text:
        return None
    try:
        return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def bootstrap_task_id(db_path: Path) -> str | None:
    """取一个已存在的 task_id，让采样协程从一开始就有东西可查。"""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT task_id FROM review_tasks "
                "WHERE status IN ('succeeded','failed') "
                "ORDER BY finished_at DESC LIMIT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def read_exec_windows(db_path: Path,
                      task_ids: list[str]) -> dict[str, tuple[str, str]]:
    """按 task_id 批量取 (started_at, finished_at) 原始文本。"""
    if not task_ids or not db_path.exists():
        return {}
    found: dict[str, tuple[str, str]] = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            for start in range(0, len(task_ids), 100):
                batch = task_ids[start:start + 100]
                marks = ",".join("?" * len(batch))
                rows = conn.execute(
                    f"SELECT task_id, started_at, finished_at FROM review_tasks "
                    f"WHERE task_id IN ({marks})", batch).fetchall()
                for task_id, started, finished in rows:
                    found[task_id] = (started, finished)
        finally:
            conn.close()
    except sqlite3.Error:
        return found
    return found


async def submit_one(client: httpx.AsyncClient, base_url: str, index: int,
                     top_k: int) -> dict[str, Any]:
    """提交一次 QA 请求并记录客户端侧时间戳与结果。

    API-4 请求体没有 top_k 字段（检索条数由服务端配置决定），
    top_k 只作为压测参数记录，不下发。
    """
    query = QUERIES[index % len(QUERIES)]
    body = {"task_type": "qa", "supply": "self_hosted", "text": query,
            "history": []}
    headers = {**HEADERS, "Idempotency-Key": str(uuid.uuid4())}
    record: dict[str, Any] = {"index": index, "query": query,
                              "submit_ts": time.time()}
    try:
        resp = await client.post(f"{base_url}/reviews", json=body,
                                 headers=headers, timeout=300.0)
        record["http_status"] = resp.status_code
        record["resp_ts"] = time.time()
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            record["task_id"] = payload.get("task_id")
            record["status"] = payload.get("status")
            record["code"] = payload.get("code")
    except Exception as exc:  # 网络/超时都算一次失败，按错误码归类
        record["resp_ts"] = time.time()
        record["http_status"] = None
        record["code"] = f"CLIENT_ERROR:{type(exc).__name__}"
    record["e2e_s"] = record["resp_ts"] - record["submit_ts"]
    return record


async def sample_queue(client: httpx.AsyncClient, base_url: str,
                       holder: dict[str, Any], samples: list[dict[str, Any]],
                       stop: asyncio.Event, interval: float = 0.1) -> None:
    """每 100ms 读一次 queue_position；task_id 缺失就沿用上一个（可能有）。"""
    started = time.time()
    while not stop.is_set():
        task_id = holder.get("task_id")
        if task_id:
            try:
                resp = await client.get(f"{base_url}/reviews/{task_id}",
                                        timeout=10.0)
                body = resp.json() if resp.status_code == 200 else {}
                position = body.get("queue_position")
                if isinstance(position, int):
                    samples.append({"t_s": round(time.time() - started, 3),
                                    "task_id": task_id,
                                    "queue_position": position})
            except Exception:
                pass  # 采样失败不污染压测主流程
        await asyncio.sleep(interval)


async def run_load(args: argparse.Namespace) -> dict[str, Any]:
    """并发提交 + 排队采样，返回统计结果。"""
    base_url = args.base_url.rstrip("/")
    db_path = Path(args.db)
    limits = httpx.Limits(max_connections=max(args.concurrency, 10),
                          max_keepalive_connections=max(args.concurrency, 10))

    holder: dict[str, Any] = {"task_id": bootstrap_task_id(db_path)}
    samples: list[dict[str, Any]] = []
    stop = asyncio.Event()

    sampler_limits = httpx.Limits(max_connections=4)
    async with httpx.AsyncClient(limits=limits) as client, \
            httpx.AsyncClient(limits=sampler_limits) as sampler_client:
        # 采样必须用独立连接池：与压测共池时 20 个在途请求会占满连接，
        # 采样被饿到 0.6s 一次，正好错过排队峰值。
        sampler = asyncio.create_task(
            sample_queue(sampler_client, base_url, holder, samples, stop))
        semaphore = asyncio.Semaphore(args.concurrency)

        async def runner(index: int) -> dict[str, Any]:
            async with semaphore:
                record = await submit_one(client, base_url, index, args.top_k)
            if record.get("task_id"):
                holder["task_id"] = record["task_id"]
            return record

        wall_start = time.time()
        results = await asyncio.gather(*[runner(i) for i in range(args.total)])
        wall_seconds = time.time() - wall_start
        # 散尾：让采样多覆盖一点收尾阶段，再停
        await asyncio.sleep(0.3)
        stop.set()
        await sampler

    # ---------- 服务端执行窗口 ----------
    task_ids = [r["task_id"] for r in results if r.get("task_id")]
    windows = read_exec_windows(db_path, task_ids)
    exec_values: list[float] = []
    for record in results:
        window = windows.get(record.get("task_id") or "")
        if not window:
            record["exec_s"] = None
            continue
        started = _parse_utc(window[0])
        finished = _parse_utc(window[1])
        if started is None or finished is None:
            record["exec_s"] = None
            continue
        # 精度 1 秒，理论非负；仍做一次防御避免时钟回拨出现负值
        record["exec_s"] = round(max(0.0, finished - started), 3)
        exec_values.append(record["exec_s"])

    # ---------- 统计 ----------
    n = len(results)
    errors = [r for r in results
              if r.get("http_status") not in (200, 202)
              or r.get("status") != "succeeded"]
    e2e_values = [r["e2e_s"] for r in results]
    queue_series = [s["queue_position"] for s in samples]
    exec_p95 = percentile(exec_values, 95)
    e2e_p95 = percentile(e2e_values, 95)
    queue_wait_p95 = (None if exec_p95 is None or e2e_p95 is None
                      else round(max(0.0, e2e_p95 - exec_p95), 3))

    result = {
        "measure_condition": MEASURE_CONDITION,
        "base_url": base_url,
        "concurrency": args.concurrency,
        "total": args.total,
        "db": str(db_path),
        "top_k_requested": args.top_k,
        "top_k_honored": False,  # API-4 无 top_k 字段，检索条数由服务端决定
        "exec_source": "review_tasks.started_at -> review_tasks.finished_at",
        # started_at 是任务创建时刻，窗口内包含 GPU 排队
        "exec_includes_queue_wait": True,
        "exec_resolution_s": 1.0,
        "percentile_method": "linear",
        "n": n,
        "error_count": len(errors),
        "error_rate": round(len(errors) / n, 4) if n else None,
        "exec_p50": percentile(exec_values, 50),
        "exec_p95": exec_p95,
        "exec_p99": percentile(exec_values, 99),
        "e2e_p50": percentile(e2e_values, 50),
        "e2e_p95": e2e_p95,
        "queue_wait_p95": queue_wait_p95,
        "wall_seconds": round(wall_seconds, 3),
        "throughput_per_hour": (round(n / wall_seconds * 3600, 2)
                                if wall_seconds > 0 else None),
        "queue_series": queue_series,
        "queue_samples": samples,
        "max_concurrent_inferred": (max(queue_series) + 1
                                    if queue_series else None),
        "errors": [{"index": r["index"], "code": r.get("code"),
                    "http_status": r.get("http_status"),
                    "query": r["query"]} for r in errors],
        "requests": [
            {"index": r["index"], "task_id": r.get("task_id"),
             "query": r["query"], "http_status": r.get("http_status"),
             "status": r.get("status"), "code": r.get("code"),
             "e2e_s": round(r["e2e_s"], 3), "exec_s": r.get("exec_s"),
             "submit_ts": round(r["submit_ts"], 3)}
            for r in results],
    }
    return result


def _verdict(passed: bool, unmet: bool = False) -> str:
    if unmet:
        return "未达成（无有效样本）"
    return "通过" if passed else "未通过"


def _fmt(value: Any, digits: int = 3) -> str:
    return "N/A" if value is None else f"{value:.{digits}f}"


def print_summary(result: dict[str, Any]) -> None:
    """人类可读摘要 + 三条判据对照。"""
    print("=" * 62)
    print("QA 链路压测结果")
    print(f"测量条件：{result['measure_condition']}")
    print(f"并发 {result['concurrency']} / 总量 {result['total']}，"
          f"总墙钟 {_fmt(result['wall_seconds'], 2)}s")
    print(f"n={result['n']}  error_count={result['error_count']}  "
          f"error_rate={result['error_rate']}")
    print(f"任务执行窗口（started_at→finished_at，含 GPU 排队，整秒）："
          f"p50={_fmt(result['exec_p50'])}s p95={_fmt(result['exec_p95'])}s "
          f"p99={_fmt(result['exec_p99'])}s")
    print(f"端到端（客户端实测，含排队）：p50={_fmt(result['e2e_p50'], 2)}s "
          f"p95={_fmt(result['e2e_p95'], 2)}s")
    print(f"两端差值（客户端开销，非真实排队）："
          f"{_fmt(result['queue_wait_p95'], 2)}s")
    print(f"吞吐：{_fmt(result['throughput_per_hour'], 2)} 任务/小时")
    series = result["queue_series"]
    print(f"queue_position 采样 {len(series)} 次，峰值 "
          f"{max(series) if series else 'N/A'}，推定并发上限 "
          f"{result['max_concurrent_inferred']}")
    if len(series) >= 3:
        picks = [0, len(series) // 2, len(series) - 1]
        print("采样点：" + "，".join(
            f"t={result['queue_samples'][i]['t_s']}s→{series[i]}" for i in picks))
    print("-" * 62)
    print("判据对照：")
    exec_p95 = result["exec_p95"]
    error_rate = result["error_rate"]
    ac71 = (exec_p95 is not None and exec_p95 <= 12.0
            and error_rate is not None and error_rate <= 0.01)
    print(f"  AC-71  exec_p95 ≤ 12s 且 error_rate ≤ 0.01："
          f"{_verdict(ac71, exec_p95 is None)}"
          f"（exec_p95={_fmt(exec_p95)}s, error_rate={error_rate}）")
    queue_wait = result["queue_wait_p95"]
    print(f"  AC-71A queue_wait_p95 ≤ 900s："
          f"{_verdict(queue_wait is not None and queue_wait <= 900.0)}"
          f"（{_fmt(queue_wait, 2)}s，queue_position 随进度递减 "
          f"{'是' if _monotonic_tail(series) else '否'}）")
    throughput = result["throughput_per_hour"]
    print(f"  AC-71B throughput ≥ 30/h："
          f"{_verdict(throughput is not None and throughput >= 30.0)}"
          f"（{_fmt(throughput, 2)}/h）")
    print("=" * 62)


def _monotonic_tail(series: list[int]) -> bool:
    """粗略判定排队是否随进度下降：后半段均值不高于前半段峰值。"""
    if len(series) < 4 or max(series) == 0:
        return False
    half = len(series) // 2
    return (sum(series[half:]) / max(1, len(series) - half)) < max(series[:half] or [0])


def main() -> int:
    parser = argparse.ArgumentParser(description="QA 链路压测（AC-71/71A/71B）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8011")
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--total", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--out", default="var/reports/load_test.json")
    parser.add_argument("--db", default="var/gips.db",
                        help="服务端 SQLite 路径，用于读执行窗口")
    args = parser.parse_args()

    random.seed()
    result = asyncio.run(run_load(args))
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print_summary(result)
    print(f"明细已写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
