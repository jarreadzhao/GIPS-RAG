"""中→英跨语言检索评测。

只用稠密分支（CLS 池化 + L2 归一化），这对应「不翻译、直接共用多语向量空间」
这一方案的最低配置。中文查询与英文 chunk 由同一个模型编码，余弦相似度直接可比。

三组对照：
- zh：中文提问（真实使用形态）
- en：同一问题的英文写法（剥离语言因素，作为上限参考）
- 两者之差即「跨语言惩罚」，是决定要不要引入查询翻译的直接依据。
"""
import json
import time
from pathlib import Path

import torch
import torch.nn.functional as F
from transformers import AutoModel, AutoTokenizer

MODEL = "models/bge-m3"
BATCH = 16
MAXLEN = 1024
TOPK = 10


def load():
    tok = AutoTokenizer.from_pretrained(MODEL)
    model = AutoModel.from_pretrained(MODEL, dtype=torch.float16).cuda().eval()
    return tok, model


@torch.no_grad()
def encode(tok, model, texts):
    out = []
    for i in range(0, len(texts), BATCH):
        b = tok(texts[i:i + BATCH], padding=True, truncation=True,
                max_length=MAXLEN, return_tensors="pt").to("cuda")
        h = model(**b).last_hidden_state[:, 0]
        out.append(F.normalize(h.float(), dim=-1).cpu())
    return torch.cat(out)


def rank(sim_row, topk=TOPK):
    v, idx = sim_row.topk(min(topk, sim_row.numel()))
    return [(int(i), float(s)) for s, i in zip(v, idx)]


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="work/chunks.jsonl")
    ap.add_argument("--queries", default="work/queries.jsonl")
    ap.add_argument("--out", default="work/eval_result.json")
    ap.add_argument("--tag", default="")
    ap.add_argument("--prefix-text", default="", help="给查询加指令前缀，用于对比指令是否缓解跨语言差距")
    ap.add_argument("--langs", default="zh,en")
    ap.add_argument("--match-field", default=None,
                    help="用哪个字段做 gold 匹配；默认与检索字段相同。测中文侧索引时传 en_text，"
                         "因为锚点是英文，gold 必须始终按英文原文判定")
    a = ap.parse_args()

    chunks = [json.loads(l) for l in open(a.corpus, encoding="utf-8")]
    queries = [json.loads(l) for l in open(a.queries, encoding="utf-8")]
    ids = [c["chunk_id"] for c in chunks]
    print(f"语料 {a.corpus}  {len(chunks)} chunk  查询 {len(queries)} 条  {a.tag}", flush=True)

    mf = getattr(a, "match_field", None) or "text"
    gold_idx = {}
    for q in queries:
        g = set()
        for k, c in enumerate(chunks):
            low = c.get(mf, c["text"]).lower()
            if any(a.lower() in low for a in q["anchors"]):
                g.add(k)
        gold_idx[q["qid"]] = g
        assert g, q["qid"]

    t0 = time.time()
    tok, model = load()
    print(f"模型加载 {time.time()-t0:.1f}s", flush=True)

    t0 = time.time()
    C = encode(tok, model, [c["text"] for c in chunks])
    t_chunk = time.time() - t0
    print(f"chunk 编码 {len(chunks)} 条，用时 {t_chunk:.2f}s（{t_chunk/len(chunks)*1000:.0f} ms/条）", flush=True)

    results = {}
    for lang in [x.strip() for x in a.langs.split(",") if x.strip()]:
        t0 = time.time()
        texts = [(a.prefix_text + " " + q[lang]).strip() if a.prefix_text else q[lang]
                 for q in queries]
        Q = encode(tok, model, texts)
        t_q = time.time() - t0
        sim = Q @ C.T

        rows = []
        for i, q in enumerate(queries):
            r = rank(sim[i])
            g = gold_idx[q["qid"]]
            first = next((pos for pos, (j, _) in enumerate(r, 1) if j in g), None)
            rows.append({
                "qid": q["qid"],
                "query": q[lang],
                "top1": ids[r[0][0]],
                "top1_score": round(r[0][1], 4),
                "first_hit_rank": first,
                "hit@1": first == 1,
                "hit@5": first is not None and first <= 5,
                "hit@10": first is not None,
                "topk": [(ids[j], round(s, 4)) for j, s in r[:5]],
            })

        n = len(rows)
        r1 = sum(r["hit@1"] for r in rows) / n
        r5 = sum(r["hit@5"] for r in rows) / n
        r10 = sum(r["hit@10"] for r in rows) / n
        mrr = sum(1 / r["first_hit_rank"] for r in rows if r["first_hit_rank"]) / n
        results[lang] = {"recall@1": r1, "recall@5": r5, "recall@10": r10,
                         "mrr@10": mrr, "queries_per_s": round(len(queries) / t_q, 1),
                         "rows": rows}
        print(f"\n[{lang}] Recall@1 {r1:.3f} | Recall@5 {r5:.3f} | "
              f"Recall@10 {r10:.3f} | MRR@10 {mrr:.3f} | {len(queries)/t_q:.1f} q/s", flush=True)

    for lang, label in (("zh", "中文提问"), ("en", "英文提问")):
        if lang not in results:
            continue
        print(f"\n=== 逐条（{label}）===")
        print(f"{'qid':4s} {'rank':>4s} {'top1':6s} {'分数':>6s}  提问 / 命中情况")
        for r in results[lang]["rows"]:
            mark = "命中" if r["hit@5"] else "未命中"
            rank_s = str(r["first_hit_rank"]) if r["first_hit_rank"] else "-"
            print(f"{r['qid']:4s} {rank_s:>4s} {r['top1']:6s} {r['top1_score']:6.3f}  "
                  f"[{mark}] {r['query']}")

    if "zh" in results and "en" in results:
        delta = results["en"]["recall@5"] - results["zh"]["recall@5"]
        print(f"\n跨语言惩罚（Recall@5）：中文 {results['zh']['recall@5']:.3f} "
              f"vs 英文 {results['en']['recall@5']:.3f}，差 {delta:+.3f}")
        d_mrr = results["en"]["mrr@10"] - results["zh"]["mrr@10"]
        print(f"跨语言惩罚（MRR@10）：中文 {results['zh']['mrr@10']:.3f} "
              f"vs 英文 {results['en']['mrr@10']:.3f}，差 {d_mrr:+.3f}")
    print(f"语料规模 {len(chunks)} chunk，Recall@5 随机基线 {5/len(chunks):.3f}")

    Path(a.out).write_text(
        json.dumps({"corpus": a.corpus, "n_chunks": len(chunks), "n_queries": len(queries),
                    "summary": {k: {m: v for m, v in results[k].items() if m != "rows"}
                                for k in results},
                    "detail": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已写入 {a.out}")


if __name__ == "__main__":
    main()
