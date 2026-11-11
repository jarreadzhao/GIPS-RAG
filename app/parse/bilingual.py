"""双语对照切分（AC-5，由 src/build_pairs.py + build_paired_chunks.py 合并而来）。

核心约束：切分以 pair 为原子，中英同进同出。
原因：两边各自按字符数切，同一内容会落在不同边界，返回的「对照」是两段
不相干的内容——这种错误不报错，只在被追问时暴露。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

HEAD_RE = re.compile(r"^(\d+(?:\.\d+)*)\s+(\S.*)$")
TERM = tuple("。！？；：…”』）】!?.")
FOOTNOTE = re.compile(r"^[∗†‡]|Conference on Neural Information Processing Systems")


@dataclass
class Para:
    """参与对齐的段落。"""

    text: str
    page: int
    is_heading: bool = False


def merge_zh(rows: list[Para]) -> list[Para]:
    """把中文文本行合并成段落：上一块不以句末标点结束就与下一块拼接。"""
    out: list[Para] = []
    buf: Para | None = None
    for r in rows:
        if r.is_heading:
            if buf:
                out.append(buf)
                buf = None
            out.append(r)
            continue
        if buf is None:
            buf = Para(r.text, r.page)
            continue
        tail = buf.text.rstrip()
        if tail.endswith(TERM):
            out.append(buf)
            buf = Para(r.text, r.page)
        else:
            buf.text = tail + r.text.lstrip()
    if buf:
        out.append(buf)
    return out


def section_of(text: str) -> str | None:
    """识别章节编号；要求标题以字母或汉字开头，避免把表格行当成章节。"""
    t = text.strip()
    m = HEAD_RE.match(t)
    if m and len(t) <= 90 and re.match(r"[A-Za-z\u4e00-\u9fff]", m.group(2)):
        return m.group(1)
    if t in ("Abstract", "摘要"):
        return "Abstract"
    return None


def group_by_section(rows: list[Para]) -> list[tuple[str, list[Para]]]:
    """按章节分组，命中 References 即停止。"""
    out: list[tuple[str, list[Para]]] = []
    cur: list[Para] = []
    sec = "FrontMatter"
    for r in rows:
        if r.text.strip() in ("References", "参考文献"):
            break
        if FOOTNOTE.search(r.text):
            continue
        s = section_of(r.text)
        if r.is_heading or s:
            if cur:
                out.append((sec, cur))
            cur, sec = [], (s or "FrontMatter")
            continue
        cur.append(r)
    if cur:
        out.append((sec, cur))
    return out


def _unit(emb: np.ndarray) -> np.ndarray:
    """把一组段落向量取均值并归一化（DP 里比较合并块用）。"""
    v = emb.mean(axis=0) if emb.ndim > 1 else emb
    n = np.linalg.norm(v)
    return v / n if n else v


def dp_align(E: np.ndarray, Z: np.ndarray, penalty: float = 0.12) -> list[tuple[int, int, int, int]]:
    """节内单调 DP，允许 1:1/2:1/1:2/3:1/1:3/4:1/1:4 步长。

    返回 [(i0, i1, j0, j1)]，索引左闭右开；终态不可达时整节压成一对。
    """
    n, m = len(E), len(Z)
    neg = -1e9
    best = np.full((n + 1, m + 1), neg)
    back = np.zeros((n + 1, m + 1), dtype=np.int8)
    best[0, 0] = 0.0
    steps = [(1, 1, 0), (2, 1, 1), (1, 2, 1), (3, 1, 2), (1, 3, 2), (4, 1, 3), (1, 4, 3)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            for k, (di, dj, pen) in enumerate(steps):
                if i - di < 0 or j - dj < 0 or best[i - di, j - dj] <= neg / 2:
                    continue
                s = float(_unit(E[i - di:i]) @ _unit(Z[j - dj:j]))
                v = best[i - di, j - dj] + s - pen * penalty
                if v > best[i, j]:
                    best[i, j] = v
                    back[i, j] = k
    if best[n, m] <= neg / 2:
        return [(0, n, 0, m)]
    pairs: list[tuple[int, int, int, int]] = []
    i, j = n, m
    while i > 0 and j > 0:
        di, dj, _ = steps[back[i, j]]
        pairs.append((i - di, i, j - dj, j))
        i -= di
        j -= dj
    pairs.reverse()
    if i > 0:
        a, b, c, d = pairs[0]
        pairs[0] = (0, b, c, d)
    if j > 0:
        a, b, c, d = pairs[-1]
        pairs[-1] = (a, b, 0, d)
    return pairs


def build_pairs(en_rows: list[Para], zh_rows: list[Para],
                encode: "callable") -> list[dict]:
    """构建 pair 表；encode 是可注入的编码函数（真实 bge-m3 或测试替身）。"""
    zh_rows = merge_zh(zh_rows)
    en_groups = group_by_section(en_rows)
    zh_map = dict(group_by_section(zh_rows))
    pairs: list[dict] = []
    for sec, en_paras in en_groups:
        zh_paras = zh_map.get(sec, [])
        if not en_paras or not zh_paras:
            for r in en_paras:
                pairs.append({"section": sec, "en": r.text, "zh": "",
                              "page_en": r.page, "page_zh": None, "merge": "en_only"})
            continue
        E = np.asarray(encode([r.text for r in en_paras]), dtype=np.float32)
        Z = np.asarray(encode([r.text for r in zh_paras]), dtype=np.float32)
        for i0, i1, j0, j1 in dp_align(E, Z):
            pairs.append({
                "section": sec,
                "en": " ".join(r.text for r in en_paras[i0:i1]),
                "zh": " ".join(r.text for r in zh_paras[j0:j1]),
                "page_en": en_paras[i0].page,
                "page_zh": zh_paras[j0].page,
                "merge": f"{i1 - i0}:{j1 - j0}",
            })
    for k, p in enumerate(pairs, 1):
        p["pair_id"] = f"P{k:04d}"
    return pairs


def build_bilingual_chunks(pairs: list[dict], *, target_en_chars: int = 550,
                           max_en_chars: int = 1100) -> list[dict]:
    """把 pair 装箱成双语 chunk：每个 chunk 同时含中英文本，pair_id 唯一。"""
    chunks: list[dict] = []
    buf: list[dict] = []
    size = 0

    def flush() -> None:
        nonlocal buf, size
        if not buf:
            return
        en = "\n".join(p["en"] for p in buf).strip()
        zh = "\n".join(p["zh"] for p in buf if p["zh"]).strip()
        if en:
            pairs_ids = [p["pair_id"] for p in buf]
            chunks.append({
                # pair_id 由首尾 pair 组成，保证唯一且可回溯到对齐结果
                "pair_id": f"{pairs_ids[0]}_{pairs_ids[-1]}",
                "text": zh + "\n" + en,
                "lang": "mixed",
                "page_en": min(p["page_en"] for p in buf),
                "page_zh": min((p["page_zh"] for p in buf if p.get("page_zh")),
                               default=None),
                "pair_ids": pairs_ids,
            })
        buf, size = [], 0

    for p in pairs:
        n = len(p["en"])
        if buf and (size >= target_en_chars or size + n > max_en_chars):
            flush()
        buf.append(p)
        size += n
    flush()
    return chunks
