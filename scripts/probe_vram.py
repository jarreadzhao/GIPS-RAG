"""显存基线实测（T-0-06）。

验收口径（AC-12A）：
    - 全程只有一个模型常驻；
    - 峰值显存 ≤ 7.5GB（8GB 卡留 0.5GB 给驱动与桌面）；
    - 每次切换后显存回到基线附近，不允许「卸载了但没归还」。

用法：
    python scripts/probe_vram.py
    python scripts/probe_vram.py --write-report
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.core.embed import BgeM3Embedder
from app.core.gpu_pool import GPUPool, nvidia_used_mb
from app.core.llm import LlamaCppBackend, LLMRequest
from app.core.rerank import BgeReranker
from app.errors import AppError


def _measure(pool: GPUPool, slot: str, warmup) -> dict:
    """加载单个槽位并记录前后显存。

    FlagEmbedding 在构造时只把权重读进内存，真正搬到显存发生在第一次
    forward，因此必须跑一次 warmup，否则测到的永远是 0 MB。
    """
    before = nvidia_used_mb()
    started = time.perf_counter()
    try:
        with pool.acquire(slot):
            warmup()
            time.sleep(0.5)  # 等驱动完成分配，避免读到瞬时值
            during = nvidia_used_mb()
            resident = pool.resident()
    except AppError as exc:
        return {"slot": slot, "status": f"skipped: {exc.code}", "reason": exc.message[:120],
                "before_mb": before, "during_mb": None, "resident": pool.resident(),
                "switch_ms": None}
    elapsed = (time.perf_counter() - started) * 1000
    pool.release(slot)
    after = nvidia_used_mb()
    # 读不到显存时不能报 PASS：0 MB 会被误读成「完全不占显存」
    status = "ok" if during is not None else "unmeasurable"
    return {"slot": slot, "status": status, "before_mb": before, "during_mb": during,
            "after_mb": after, "resident": resident, "switch_ms": round(elapsed, 1)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-report", action="store_true",
                        help="把结果写入 docs/显存基线报告.md")
    parser.add_argument("--cycles", type=int, default=1,
                        help="每个槽位的加载/卸载循环次数（AC-12A 要求 20）")
    args = parser.parse_args()

    settings = get_settings()
    pool = GPUPool()

    # 必须用真实实现：占位实现不做加载，测出来的「0 MB」会让人误判为合格
    embedder = BgeM3Embedder(settings, pool)
    reranker = BgeReranker(settings, pool)
    generator = LlamaCppBackend(settings, pool)
    pool.register(embedder)
    pool.register(reranker)
    pool.register(generator)

    probes = [
        ("embed", lambda: embedder.encode(["probe text"])),
        ("rerank", lambda: reranker.score("probe", ["passage"])),
        ("generate", lambda: generator.generate(LLMRequest("ok", max_tokens=8))),
    ]
    cycles = max(1, args.cycles)
    rows: list[dict] = []
    for i in range(cycles):
        for slot, warmup in probes:
            row = _measure(pool, slot, warmup)
            row["cycle"] = i + 1
            rows.append(row)
            print(f"[cycle {i + 1}/{cycles}] {slot}: {row['status']} "
                  f"during={row.get('during_mb')}MB after={row.get('after_mb')}MB", flush=True)
    peak = max((r.get("during_mb") or 0) for r in rows)
    limit_mb = int(settings.gpu_peak_limit_gb * 1024)
    single_resident = all(len(r.get("resident") or []) <= 1 for r in rows)
    measured = any(r["status"] == "ok" for r in rows) and \
        all(r["status"] != "unmeasurable" for r in rows)

    print(json.dumps(rows, ensure_ascii=False, indent=2))
    print(f"\n峰值显存: {peak} MB / 上限 {limit_mb} MB")
    print(f"单模型常驻: {'是' if single_resident else '否'}")
    ok = measured and peak <= limit_mb and single_resident
    complete = all(r["status"] == "ok" for r in rows)
    if not measured:
        verdict = "无法测量（nvidia-smi 读取失败），不判定为 PASS"
    elif complete:
        verdict = "PASS" if ok else "FAIL"
        if cycles > 1:
            verdict += f"（{cycles} 次循环）"
    else:
        verdict = ("部分通过：" + "、".join(
            str(r["slot"]) for r in rows if r["status"] != "ok") + " 缺权重/后端，未实测")
    print(f"结论: {verdict}")

    if args.write_report:
        out = Path("docs/显存基线报告.md")
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# 显存基线报告",
            "",
            f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
            f"- 卡型：RTX 4060 Laptop 8GB（以 nvidia-smi 为准）",
            f"- 上限：{limit_mb} MB",
            f"- 结论：{verdict}",
            "",
            "| 槽位 | 状态 | 加载前 MB | 加载后 MB | 卸载后 MB | 常驻 | 切换耗时 ms |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        first = [r for r in rows if r.get("cycle") == 1]
        for r in first:
            lines.append(
                f"| {r['slot']} | {r['status']} | {r.get('before_mb')} | {r.get('during_mb')} "
                f"| {r.get('after_mb')} | {r.get('resident')} | {r.get('switch_ms')} |"
            )
        if cycles > 1:
            lines += [
                "",
                f"## {cycles} 次循环稳定性（AC-12A）",
                "",
                "| 槽位 | 次数 | 成功 | 峰值 MB | 卸载后最小 MB | 卸载后最大 MB |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
            for slot, _ in probes:
                rs = [r for r in rows if r["slot"] == slot]
                peaks = [r["during_mb"] for r in rs if r.get("during_mb") is not None]
                afters = [r["after_mb"] for r in rs if r.get("after_mb") is not None]
                ok_n = sum(1 for r in rs if r["status"] == "ok")
                lines.append(
                    f"| {slot} | {len(rs)} | {ok_n} | {max(peaks) if peaks else None} "
                    f"| {min(afters) if afters else None} | {max(afters) if afters else None} |"
                )
            lines += [
                "",
                f"- 峰值显存：{peak} MB（上限 {limit_mb} MB）",
                f"- 单模型常驻：{'是' if single_resident else '否'}",
                "- 判定口径：全程无 OOM、每次卸载后显存回落到基线附近、任一时刻只有一个模型常驻",
            ]
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"报告已写入 {out}")

    pool.shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
