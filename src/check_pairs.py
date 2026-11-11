# -*- coding: utf-8 -*-
"""对齐质量的客观代理指标 + 两种索引的配对检验"""
import json
import math
from math import comb

import numpy as np
import torch
from transformers import AutoModel, AutoTokenizer

MODEL = "models/bge-m3"


def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def mcnemar(b, c):
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def pair_similarity():
    pairs = [json.loads(l) for l in open("work/pairs.jsonl", encoding="utf-8")]
    tok = AutoTokenizer.from_pretrained(MODEL)
    m = AutoModel.from_pretrained(MODEL, torch_dtype=torch.float16).cuda().eval()

    @torch.no_grad()
    def enc(texts):
        b = tok(texts, padding=True, truncation=True, max_length=512,
                return_tensors="pt").to("cuda")
        v = m(**b).last_hidden_state[:, 0].float()
        return torch.nn.functional.normalize(v, dim=-1).cpu().numpy()

    E = enc([p["en"] for p in pairs])
    Z = enc([p["zh"] for p in pairs])
    sims = (E * Z).sum(1)
    print("=== 对齐相似度（EN 段 vs 其配对的 ZH 段）===")
    q = np.percentile(sims, [5, 10, 25, 50, 75, 90])
    print(f"  min={sims.min():.3f}  p5={q[0]:.3f} p10={q[1]:.3f} p25={q[2]:.3f} "
          f"中位={q[3]:.3f} p75={q[4]:.3f} p90={q[5]:.3f} max={sims.max():.3f}")

    # 对照：随机错配（同章节内错一位）能拿到多少分
    rng = np.random.default_rng(0)
    perm = rng.permutation(len(pairs))
    rand = (E * Z[perm]).sum(1)
    print(f"  随机错配基线 中位={np.median(rand):.3f}  p95={np.percentile(rand, 95):.3f}")
    print(f"  低于随机 p95 的 pair 数：{(sims < np.percentile(rand, 95)).sum()} / {len(pairs)}")

    idx = np.argsort(sims)[:5]
    print("\n  相似度最低的 5 对（需人工核对）：")
    for i in idx:
        p = pairs[i]
        print(f"   {p['pair_id']} [{p['section']}] {p['merge']} sim={sims[i]:.3f}")
        print(f"      EN: {p['en'][:76]}")
        print(f"      ZH: {p['zh'][:46]}")
    return sims


def compare():
    print("\n=== 中文查询：英文索引 vs 中文索引 ===")
    a = json.load(open("work/bil_pc_en.jsonl", encoding="utf-8"))["detail"]["zh"]["rows"]
    b = json.load(open("work/bil_pc_zh.jsonl", encoding="utf-8"))["detail"]["zh"]["rows"]
    A = {r["qid"]: r for r in a}
    B = {r["qid"]: r for r in b}
    n = len(A)
    for m in ("hit@1", "hit@5", "hit@10"):
        ka = sum(A[q][m] for q in A)
        kb = sum(B[q][m] for q in B)
        la, ha = wilson(ka, n)
        lb, hb = wilson(kb, n)
        only_b = sum(1 for q in A if not A[q][m] and B[q][m])   # 只有中文索引命中
        only_a = sum(1 for q in A if A[q][m] and not B[q][m])   # 只有英文索引命中
        print(f"  {m:6s} EN索引 {ka}/{n}={ka/n:.3f} 95%CI[{la:.3f},{ha:.3f}] | "
              f"ZH索引 {kb}/{n}={kb/n:.3f} 95%CI[{lb:.3f},{hb:.3f}] | "
              f"仅ZH命中 {only_b} / 仅EN命中 {only_a}，McNemar p={mcnemar(only_a, only_b):.3f}")


if __name__ == "__main__":
    pair_similarity()
    compare()
