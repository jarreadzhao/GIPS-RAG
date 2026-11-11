"""中英段落级对齐：把两个版本压成可互相引用的 pair 表。

为什么要做这件事：
两版 PDF 各自切分后，同一个内容落在不同的块边界上。如果两边各建索引、
各自返回 top-k，用户看到的"对照"其实是两段不相干的内容——而这个错误
不会报错，只会让人在追问时发现引用对不上。

对齐分三步：
1. 重建段落。中文版的文本层是"文本行级"的，一个句子常被拆成两块
   （"…复制本文" / "中表格和图形的许可。"）。判据：上一块不以句末标点
   结束，就与下一块合并，允许跨页（跨页断词同样要接上）。
2. 按章节分窗。22 个编号章节在中英两侧都完整存在，先按编号把搜索空间
   限制在本节内，避免跨节错配——跨节错配是对齐里最难看的一种错。
3. 节内单调 DP。允许 1:1 / 2:1 / 1:2 / 3:1 / 1:3 五种步长，
   代价 = 语义相似度 × 长度比先验 − 合并罚分。单调约束保证不交叉。
"""
import json
import re
import sys
from pathlib import Path

import numpy as np
import torch
from transformers import AutoModel, AutoTokenizer

MODEL = "models/bge-m3"
HEAD_RE = re.compile(r"^(\d+(?:\.\d+)*)\s+(\S.*)$")
TERM = tuple("。！？；：…”』）】!?.")
# 脚注与页脚：中文版没有对应段落，留在语料里只会污染对齐
FOOTNOTE = re.compile(r"^[∗†‡]|Conference on Neural Information Processing Systems")


# ---------------- 段落重建 ----------------
def merge_zh(rows):
    """把文本行级的块合并成段落。"""
    out, buf = [], None
    for r in rows:
        if r["is_heading"]:
            if buf:
                out.append(buf)
                buf = None
            out.append(r)
            continue
        if buf is None:
            buf = dict(r)
            continue
        tail = buf["text"].rstrip()
        if tail.endswith(TERM):
            out.append(buf)
            buf = dict(r)
        else:
            buf["text"] = tail + r["text"].lstrip()
            buf["n_chars"] = len(buf["text"])
            buf["page_end"] = r["page"]
    if buf:
        out.append(buf)
    return out


def sec_of(text):
    """认章节编号。

    必须要求标题部分以字母或汉字开头：表格被拆成文本块后会出现
    "1 512 512 5.29 24.9" 这种行，旧正则会把它当成"第 1 节"的标题，
    导致整节内容被挂到第 9 页的表格上。这类错配不会报错，只会让
    对齐结果整体错位。
    """
    t = text.strip()
    m = HEAD_RE.match(t)
    if m and len(t) <= 90 and re.match(r"[A-Za-z\u4e00-\u9fff]", m.group(2)):
        return m.group(1)
    if t in ("Abstract", "摘要"):
        return "Abstract"
    return None


def group(rows):
    """按章节编号分组，返回 [(sec, [para...])]，保持出现顺序。"""
    out, cur, sec = [], [], "FrontMatter"
    for r in rows:
        if r["text"].strip() in ("References", "参考文献"):
            break
        if FOOTNOTE.search(r["text"]):
            continue
        if r.get("is_heading") or sec_of(r["text"]):
            s = sec_of(r["text"]) or "FrontMatter"
            if cur:
                out.append((sec, cur))
            cur, sec = [], s
            continue
        cur.append(r)
    if cur:
        out.append((sec, cur))
    return out


# ---------------- 编码 ----------------
def load_model():
    tok = AutoTokenizer.from_pretrained(MODEL)
    m = AutoModel.from_pretrained(MODEL, torch_dtype=torch.float16).cuda().eval()
    return tok, m


@torch.no_grad()
def encode(tok, model, texts, bs=32, maxlen=512):
    vs = []
    for i in range(0, len(texts), bs):
        b = tok(texts[i:i + bs], padding=True, truncation=True,
                max_length=maxlen, return_tensors="pt").to("cuda")
        o = model(**b)
        v = torch.nn.functional.normalize(o.last_hidden_state[:, 0].float(), dim=-1)
        vs.append(v.cpu().numpy())
    return np.vstack(vs) if vs else np.zeros((0, 1024), dtype=np.float32)


# ---------------- 单调 DP ----------------
def dp_align(E, Z, penalty=0.12, lw=0.05):
    """返回 [(i0,i1, j0,j1)]，索引左闭右开。E/Z 为已归一化向量矩阵。"""
    n, m = len(E), len(Z)
    NEG = -1e9
    best = np.full((n + 1, m + 1), NEG)
    back = np.zeros((n + 1, m + 1), dtype=np.int8)
    best[0, 0] = 0.0

    def sim(emb):
        e = emb[0]
        if e.ndim == 1:
            e = e[None, :]
        v = e.mean(axis=0)
        nv = np.linalg.norm(v)
        return v / nv if nv else v

    steps = [(1, 1, 0), (2, 1, 1), (1, 2, 1), (3, 1, 2), (1, 3, 2), (4, 1, 3), (1, 4, 3)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            for di, dj, pen in steps:
                if i - di < 0 or j - dj < 0:
                    continue
                prev = best[i - di, j - dj]
                if prev <= NEG / 2:
                    continue
                s = float(sim(E[i - di:i]) @ sim(Z[j - dj:j]))
                v = prev + s - pen * penalty
                if v > best[i, j]:
                    best[i, j] = v
                    back[i, j] = steps.index((di, dj, pen))
    # 终态不可达时（两侧段数相差过大，步长覆盖不到），整节压成一对。
    # 不能让它带着无效回溯继续走：那会产出一条"形式完整但内容错位"的 pair，
    # 比整节粗对齐更危险，因为看不出问题。
    if best[n, m] <= NEG / 2:
        return [(0, n, 0, m)], float("-inf"), "fallback"
    # 回溯
    pairs, i, j = [], n, m
    while i > 0 and j > 0:
        di, dj, _ = steps[back[i, j]]
        pairs.append((i - di, i, j - dj, j))
        i -= di
        j -= dj
    pairs.reverse()
    if i > 0:  # 余下的英文段并入首对，避免静默丢弃
        a, b, c, d = pairs[0]
        pairs[0] = (0, b, c, d)
    if j > 0:
        a, b, c, d = pairs[-1]
        pairs[-1] = (a, b, 0, d)
    return pairs, float(best[n, m]), "ok"


def main():
    en_path = "work/paras.jsonl"
    zh_path = "work/paras_zh.jsonl"
    out = Path("work/pairs.jsonl")

    en_rows = [json.loads(l) for l in open(en_path, encoding="utf-8")]
    zh_rows = merge_zh([json.loads(l) for l in open(zh_path, encoding="utf-8")])
    print(f"中文块 {len([r for r in json.load(open(zh_path, encoding='utf-8'))]) if False else ''}"
          f"合并后中文段落 {len(zh_rows)} | 英文段落 {len(en_rows)}")

    G_en = dict(group(en_rows))
    G_zh = dict(group(zh_rows))
    secs = [s for s, _ in group(en_rows)]
    seen, order = set(), []
    for s in secs:
        if s not in seen:
            seen.add(s)
            order.append(s)

    tok, model = load_model()
    pairs, stats = [], []
    for sec in order:
        E_rows, Z_rows = G_en.get(sec, []), G_zh.get(sec, [])
        if not E_rows or not Z_rows:
            stats.append((sec, len(E_rows), len(Z_rows), 0, "缺一侧"))
            for k, r in enumerate(E_rows):
                pairs.append({"section": sec, "en": r["text"], "zh": "", "page_en": r["page"],
                              "page_zh": None, "merge": "en_only"})
            continue
        Ev = encode(tok, model, [r["text"] for r in E_rows])
        Zv = encode(tok, model, [r["text"] for r in Z_rows])
        al, score, mode = dp_align(Ev, Zv)
        covered_e = sum(i1 - i0 for i0, i1, _, _ in al)
        covered_z = sum(j1 - j0 for _, _, j0, j1 in al)
        for i0, i1, j0, j1 in al:
            pairs.append({
                "section": sec,
                "en": " ".join(E_rows[i]["text"] for i in range(i0, i1)),
                "zh": " ".join(Z_rows[j]["text"] for j in range(j0, j1)),
                "page_en": E_rows[i0]["page"],
                "page_zh": Z_rows[j0]["page"],
                "merge": f"{i1 - i0}:{j1 - j0}",
            })
        stats.append((sec, len(E_rows), len(Z_rows), len(al),
                      f"{mode} 覆盖E{covered_e}/{len(E_rows)} Z{covered_z}/{len(Z_rows)}"))

    for k, p in enumerate(pairs, 1):
        p["pair_id"] = f"P{k:04d}"
    with open(out, "w", encoding="utf-8") as f:
        for p in pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    print(f"\n生成 pair {len(pairs)} 条 -> {out}")
    print(f"{'章节':10s} {'E段':>4s} {'Z段':>4s} {'pair':>5s}  说明")
    for s, a, b, c, note in stats:
        print(f"  {s:8s} {a:4d} {b:4d} {c:5d}  {note}")
    mr = {}
    for p in pairs:
        mr[p["merge"]] = mr.get(p["merge"], 0) + 1
    print("\n步长分布：", dict(sorted(mr.items(), key=lambda x: -x[1])))
    ok = sum(1 for p in pairs if p["zh"])
    print(f"双语齐备 {ok}/{len(pairs)}（{ok / len(pairs):.1%}）")


if __name__ == "__main__":
    main()
