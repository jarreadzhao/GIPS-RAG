"""等权重落盘完整后自动跑评测。

存在问题：下载与评测分属两个后台任务，人工接力容易踩到「拼接还没结束就加载」。
这里用连续多次尺寸一致作为就绪判据，超时则报错退出，不做无限等待。
"""
import subprocess
import sys
import time
from pathlib import Path

TOTAL = 2271145830
TARGET = Path("models/bge-m3/pytorch_model.bin")
MAX_WAIT = 3600


def size():
    return TARGET.stat().st_size if TARGET.exists() else 0


def main():
    t0 = time.time()
    stable = 0
    while time.time() - t0 < MAX_WAIT:
        s = size()
        if s == TOTAL:
            stable += 1
            if stable >= 3:
                print(f"权重就绪 {s} 字节（{time.time()-t0:.0f}s）", flush=True)
                break
        else:
            stable = 0
        time.sleep(20)
    else:
        print(f"超时：等待 {MAX_WAIT}s 后仍是 {size()}/{TOTAL} 字节", flush=True)
        sys.exit(1)

    r = subprocess.run([sys.executable, "-u", "src/eval_retrieval.py"])
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
