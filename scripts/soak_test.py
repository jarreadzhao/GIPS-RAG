"""QA 链路浸泡测试（AC-74）。

AC-74 原文要求连续浸泡 8 小时；本脚本默认 15 分钟，是缩短版，
只能看短期趋势，不能替代 8 小时结论——输出里 ac74_met 恒为 False，
除非实际时长达到 8 小时。

采样对象：
    后端进程：RSS 与句柄数（Windows 用 num_handles）。
    自身进程：同一口径，用于区分「压测端泄漏」与「服务端泄漏」。
RSS 用 psutil 的 rss（物理常驻集），不包含被换出的页。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import psutil

QUERIES = [
    "portfolio company 的定义在哪个条款",
    "1.A.4 要求什么",
    "carve-out 的披露要求",
    "GIPS 对组合收益披露有什么要求",
    "机构必须记录哪些政策和程序",
]
HEADERS = {"X-Role": "reviewer", "X-Dept": "compliance"}
MEASURE_CONDITION = ("127.0.0.1:8011, bge-m3 + bge-reranker-v2-m3, "
                     "QA 抽取式链路（无生成模型）")
AC74_FULL_HOURS = 8.0


def find_backend_pid(port: int) -> int | None:
    """/healthz 不返回 pid，按监听端口反查进程号。"""
    try:
        for conn in psutil.net_connections(kind="inet"):
            if (conn.laddr and conn.laddr.port == port
                    and conn.status == psutil.CONN_LISTEN and conn.pid):
                return conn.pid
    except (psutil.AccessDenied, OSError):
        pass
    # netstat 兜底：非管理员时 psutil 可能拿不到别的进程的连接
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.splitlines():
        parts = line.split()
        if (len(parts) >= 5 and parts[1].endswith(f":{port}")
                and parts[3].upper() == "LISTENING"):
            try:
                return int(parts[4])
            except ValueError:
                return None
    return None


def sample_process(pid: int | None) -> dict[str, Any]:
    """读一次 RSS(MB) 与句柄数；进程不在或无权访问时返回 None 字段。"""
    if pid is None:
        return {"pid": None, "rss_mb": None, "handles": None}
    try:
        proc = psutil.Process(pid)
        return {"pid": pid,
                "rss_mb": round(proc.memory_info().rss / 1024 / 1024, 2),
                "handles": proc.num_handles()}
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return {"pid": pid, "rss_mb": None, "handles": None}


def _growth_pct(first: float | None, last: float | None) -> float | None:
    if first in (None, 0) or last is None:
        return None
    return round((last - first) / first * 100, 2)


def _delta(first: int | None, last: int | None) -> int | None:
    if first is None or last is None:
        return None
    return last - first


def _peak(values: list[float | int | None]) -> float | int | None:
    present = [v for v in values if v is not None]
    return max(present) if present else None


def run_soak(args: argparse.Namespace) -> dict[str, Any]:
    """循环发 QA 请求，期间定期采样资源。"""
    base_url = args.base_url.rstrip("/")
    backend_pid = find_backend_pid(args.port)
    self_pid = psutil.Process().pid
    deadline = time.time() + args.minutes * 60

    samples: list[dict[str, Any]] = []
    latencies: list[float] = []
    errors: list[dict[str, Any]] = []
    total = 0
    started = time.time()

    def take_sample(tag: str) -> None:
        samples.append({"t_s": round(time.time() - started, 1), "tag": tag,
                        "backend": sample_process(backend_pid),
                        "self": sample_process(self_pid)})

    take_sample("start")
    last_sample = time.time()

    with httpx.Client(timeout=120.0) as client:
        while time.time() < deadline:
            query = QUERIES[total % len(QUERIES)]
            headers = {**HEADERS, "Idempotency-Key": str(uuid.uuid4())}
            body = {"task_type": "qa", "supply": "self_hosted",
                    "text": query, "history": []}
            total += 1
            begin = time.time()
            try:
                resp = client.post(f"{base_url}/reviews", json=body,
                                   headers=headers)
                elapsed = time.time() - begin
                payload = resp.json() if resp.content else {}
                ok = resp.status_code in (200, 202) and \
                    payload.get("status") == "succeeded"
                if ok:
                    latencies.append(elapsed)
                else:
                    errors.append({"n": total, "http_status": resp.status_code,
                                   "code": payload.get("code"),
                                   "at_s": round(time.time() - started, 1)})
            except Exception as exc:
                errors.append({"n": total, "http_status": None,
                               "code": f"CLIENT_ERROR:{type(exc).__name__}",
                               "at_s": round(time.time() - started, 1)})
            if time.time() - last_sample >= args.sample_interval:
                take_sample("periodic")
                last_sample = time.time()
            # 留出间隔；若已到截止时间就不再白等
            remain = deadline - time.time()
            if remain > 0:
                time.sleep(min(args.interval, remain))

    take_sample("end")

    backend_samples = [s["backend"] for s in samples]
    self_samples = [s["self"] for s in samples]
    b_first = backend_samples[0] if backend_samples else {}
    b_last = backend_samples[-1] if backend_samples else {}
    s_first = self_samples[0] if self_samples else {}
    s_last = self_samples[-1] if self_samples else {}
    latencies.sort()

    def pct(p: float) -> float | None:
        if not latencies:
            return None
        return round(latencies[min(len(latencies) - 1,
                                   int((len(latencies) - 1) * p / 100))], 3)

    duration_min = round((time.time() - started) / 60, 2)
    result = {
        "measure_condition": MEASURE_CONDITION,
        "base_url": base_url,
        "duration_minutes": duration_min,
        "interval_s": args.interval,
        "sample_interval_s": args.sample_interval,
        "backend_pid": backend_pid,
        "n_requests": total,
        "n_failed": len(errors),
        "error_rate": round(len(errors) / total, 4) if total else None,
        "latency_p50_s": pct(50),
        "latency_p95_s": pct(95),
        "backend": {
            "rss_first_mb": b_first.get("rss_mb"),
            "rss_last_mb": b_last.get("rss_mb"),
            "rss_peak_mb": _peak([s.get("rss_mb") for s in backend_samples]),
            "rss_growth_pct": _growth_pct(b_first.get("rss_mb"),
                                          b_last.get("rss_mb")),
            "handles_first": b_first.get("handles"),
            "handles_last": b_last.get("handles"),
            "handles_peak": _peak([s.get("handles") for s in backend_samples]),
            "handles_growth": _delta(b_first.get("handles"),
                                     b_last.get("handles")),
        },
        "self": {
            "rss_first_mb": s_first.get("rss_mb"),
            "rss_last_mb": s_last.get("rss_mb"),
            "rss_peak_mb": _peak([s.get("rss_mb") for s in self_samples]),
            "rss_growth_pct": _growth_pct(s_first.get("rss_mb"),
                                          s_last.get("rss_mb")),
            "handles_first": s_first.get("handles"),
            "handles_last": s_last.get("handles"),
            "handles_growth": _delta(s_first.get("handles"),
                                     s_last.get("handles")),
        },
        "ac74_required_hours": AC74_FULL_HOURS,
        # 未达 8 小时一律如实标 False，缩短版不能冒充原文要求
        "ac74_met": duration_min >= AC74_FULL_HOURS * 60,
        "samples": samples,
        "errors": errors,
    }
    return result


def print_summary(result: dict[str, Any]) -> None:
    backend = result["backend"]
    own = result["self"]
    print("=" * 62)
    print("QA 链路浸泡结果（缩短版）")
    print(f"测量条件：{result['measure_condition']}")
    print(f"时长 {result['duration_minutes']} 分钟，请求间隔 "
          f"{result['interval_s']}s，资源采样间隔 {result['sample_interval_s']}s")
    print(f"后端 pid={result['backend_pid']}  "
          f"请求 {result['n_requests']} 次，失败 {result['n_failed']} 次"
          f"（error_rate={result['error_rate']}）")
    print(f"请求时延 p50={result['latency_p50_s']}s p95={result['latency_p95_s']}s")
    print(f"后端 RSS：首 {backend['rss_first_mb']}MB → 末 "
          f"{backend['rss_last_mb']}MB，峰值 {backend['rss_peak_mb']}MB，"
          f"增长率 {backend['rss_growth_pct']}%")
    print(f"后端句柄：首 {backend['handles_first']} → 末 "
          f"{backend['handles_last']}（峰值 {backend['handles_peak']}），"
          f"增长 {backend['handles_growth']}")
    print(f"自身 RSS：首 {own['rss_first_mb']}MB → 末 {own['rss_last_mb']}MB，"
          f"峰值 {own['rss_peak_mb']}MB，增长率 {own['rss_growth_pct']}%；"
          f"句柄增长 {own['handles_growth']}")
    print("-" * 62)
    verdict = "通过" if result["ac74_met"] else "未达成"
    print(f"  AC-74 连续浸泡 {AC74_FULL_HOURS} 小时：{verdict}"
          f"（本次 {result['duration_minutes']} 分钟，为缩短版）")
    print("=" * 62)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="QA 链路浸泡测试。注意：AC-74 原文要求 8 小时，"
                    "本脚本默认 15 分钟属缩短版，不能替代 8 小时结论。")
    parser.add_argument("--minutes", type=float, default=15.0,
                        help="浸泡时长（分钟）；AC-74 原文要求 480 分钟")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="两次 QA 请求之间的间隔（秒）")
    parser.add_argument("--sample-interval", type=float, default=30.0,
                        help="资源采样间隔（秒）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8011")
    parser.add_argument("--port", type=int, default=8011,
                        help="后端监听端口，用于反查 pid")
    parser.add_argument("--out", default="var/reports/soak.json")
    args = parser.parse_args()

    if not sys.platform.startswith("win"):
        print("提示：num_handles 仅 Windows 可用，其他平台句柄字段会是 None")

    result = run_soak(args)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print_summary(result)
    print(f"明细已写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
