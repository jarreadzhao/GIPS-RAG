"""分段并行下载 bge-m3 权重。

原因：modelscope CDN 单连接被限速到约 125KB/s（实测 4 路并发各 125KB/s，
总带宽不是瓶颈），2.1GB 单连接要三个多小时。故按字节区间切 16 段并发拉，
单段失败独立重试，最后按序拼接。

用法：python src/dl_parallel.py <已有字节数识别起点>
"""
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

URL = ("https://modelscope.cn/api/v1/models/BAAI/bge-m3/repo"
       "?Revision=master&FilePath=pytorch_model.bin")
TOTAL = 2271145830
NSEG = 16
TARGET = Path("models/bge-m3/pytorch_model.bin")
PARTS = Path("models/bge-m3/_parts")

print_lock = threading.Lock()


def log(msg):
    with print_lock:
        print(msg, flush=True)


def fetch(idx, start, end, tries=6):
    part = PARTS / f"part_{idx:02d}.bin"
    want = end - start + 1
    if part.exists() and part.stat().st_size == want:
        log(f"  seg{idx:02d} 已完成，跳过")
        return True
    for k in range(1, tries + 1):
        if part.exists():
            part.unlink()
        t0 = time.time()
        p = subprocess.run(
            ["curl", "-s", "-L", "-f", "-r", f"{start}-{end}",
             "-o", str(part), URL],
            capture_output=True,
        )
        got = part.stat().st_size if part.exists() else 0
        dt = max(time.time() - t0, 1e-6)
        if got == want:
            log(f"  seg{idx:02d} ok {got/1e6:.1f}MB {got/dt/1024:.0f}KB/s")
            return True
        log(f"  seg{idx:02d} 第{k}次失败 rc={p.returncode} 得到 {got}/{want}")
        time.sleep(3)
    return False


def main():
    PARTS.mkdir(parents=True, exist_ok=True)
    base = TARGET.stat().st_size if TARGET.exists() else 0
    print(f"已有 {base/1e9:.3f}GB / 共 {TOTAL/1e9:.3f}GB，剩余需下 {(TOTAL-base)/1e6:.0f}MB")

    remain = TOTAL - base
    step = math.ceil(remain / NSEG)
    jobs = []
    for i in range(NSEG):
        s = base + i * step
        e = min(s + step - 1, TOTAL - 1)
        if s > e:
            break
        jobs.append((i, s, e))

    print(f"切 {len(jobs)} 段，每段约 {step/1e6:.0f}MB，并发下载中…")
    t0 = time.time()
    threads = [threading.Thread(target=fetch, args=j) for j in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    missing = []
    for i, s, e in jobs:
        p = PARTS / f"part_{i:02d}.bin"
        if not p.exists() or p.stat().st_size != e - s + 1:
            missing.append(p.name)
    if missing:
        print(f"失败：{len(missing)} 段未完成 {missing[:5]}")
        sys.exit(1)

    print(f"全部到位，用时 {time.time()-t0:.0f}s，开始拼接…")
    with open(TARGET, "ab") as out:
        for i, _, _ in jobs:
            with open(PARTS / f"part_{i:02d}.bin", "rb") as f:
                while True:
                    b = f.read(1 << 22)
                    if not b:
                        break
                    out.write(b)

    size = TARGET.stat().st_size
    print(f"拼接完成 {size} 字节，期望 {TOTAL}，{'一致' if size == TOTAL else '不一致！'}")
    if size == TOTAL:
        for f in PARTS.glob("part_*.bin"):
            f.unlink()
        PARTS.rmdir()


if __name__ == "__main__":
    main()
