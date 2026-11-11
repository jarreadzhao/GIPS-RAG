"""把 paras.jsonl 切成检索单元（chunk）。

设计约束：
1. 只在段落边界切，绝不在段落内部切 —— 保证 chunk 文本与 PDF 原文逐字一致，
   引用时能精确回指页码（span 精确匹配是后面校验的前提）。
2. 不设重叠。重叠会把同一段文字放进两个 chunk，锚点定位出现歧义；
   代价是边界处的语义可能被截断，用段落对齐换取可验证性。
3. 跟踪章节标题，写入 section 字段，供检索结果给出定位信息。
4. 丢弃 References 之后的参考文献条目，它不是问答语料。
"""
import json
import re
import sys
from pathlib import Path

TARGET = 550          # 目标字符数
MAXC = 1100           # 单 chunk 上限，超过则强制在该段结束处断开
MINC = 60             # 低于此长度的尾块并入前一块

HEAD_RE = re.compile(r"^(\d+(?:\.\d+)*)\s+([A-Z][A-Za-z0-9 ,\-:'’]{2,70})$")
APPENDIX_RE = re.compile(r"^([A-Z])\.\d+\s+")


def load(path):
    return [json.loads(l) for l in open(path, encoding="utf-8")]


def is_heading(t):
    t = t.strip()
    if len(t) > 80:
        return None
    m = HEAD_RE.match(t)
    # 标题部分必须以字母或汉字开头。否则表格被拆成文本块后的行
    # （如 "1 512 512 5.29 24.9"）会被当成"第 1 节"标题，
    # 既污染 section 字段，又把整行数字从语料里静默删掉。
    if m and re.match(r"[A-Za-z\u4e00-\u9fff]", m.group(2)):
        return m.group(0)
    if APPENDIX_RE.match(t):
        return t
    if t in ("Abstract", "References", "Conclusion"):
        return t
    return None


def build(rows, mode="coarse"):
    # 先剔除 References 之后的条目
    out_rows = []
    in_refs = False
    for r in rows:
        if is_heading(r["text"]) == "References":
            in_refs = True
            continue
        if in_refs:
            continue
        out_rows.append(r)

    if mode == "fine":
        # 细粒度：一段一块，不做合并。用于压测检索难度（语料块越小，跨语言越难）。
        chunks = []
        section = "Front Matter"
        for r in out_rows:
            t = r["text"].strip()
            h = is_heading(t)
            if h:
                section = h
                continue
            if len(t) < 40:
                continue
            chunks.append({
                "chunk_id": f"f{len(chunks) + 1:03d}",
                "section": section,
                "page_start": r["page"],
                "page_end": r["page"],
                "text": t,
                "n_chars": len(t),
            })
        return chunks

    chunks = []
    buf, buf_pages, buf_n = [], [], 0
    section = "Front Matter"
    cur_chars = 0

    def flush():
        nonlocal buf, buf_pages, buf_n, cur_chars
        if not buf:
            return
        text = "\n".join(buf).strip()
        if text:
            chunks.append({
                "chunk_id": f"c{len(chunks) + 1:03d}",
                "section": section,
                "page_start": min(buf_pages),
                "page_end": max(buf_pages),
                "text": text,
                "n_chars": len(text),
            })
        buf, buf_pages, buf_n, cur_chars = [], [], 0, 0

    for r in out_rows:
        t = r["text"].strip()
        h = is_heading(t)
        if h:
            flush()
            section = h
            continue
        if cur_chars >= TARGET or (cur_chars + len(t) > MAXC and buf):
            flush()
        buf.append(t)
        buf_pages.append(r["page"])
        buf_n += 1
        cur_chars += len(t)
    flush()

    # 尾块过短则并入上一块
    if len(chunks) >= 2 and chunks[-1]["n_chars"] < MINC:
        tail = chunks.pop()
        chunks[-1]["text"] += "\n" + tail["text"]
        chunks[-1]["n_chars"] = len(chunks[-1]["text"])
        chunks[-1]["page_end"] = max(chunks[-1]["page_end"], tail["page_end"])
    return chunks


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["coarse", "fine"], default="coarse")
    ap.add_argument("--src", default="work/paras.jsonl")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    src = Path(a.src)
    dst = Path(a.out or ("work/chunks.jsonl" if a.mode == "coarse" else "work/chunks_fine.jsonl"))
    rows = load(src)
    chunks = build(rows, a.mode)
    with open(dst, "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    L = [c["n_chars"] for c in chunks]
    print(f"模式 {a.mode} | 段落 {len(rows)} -> chunk {len(chunks)}")
    print(f"字符数 min/中位/max = {min(L)} / {sorted(L)[len(L)//2]} / {max(L)}")
    print(f"总字符 {sum(L)} -> {dst}")
    print("\n各 chunk 一览：")
    for c in chunks:
        head = c["text"][:58].replace("\n", " ")
        print(f"  {c['chunk_id']} p{c['page_start']}-{c['page_end']} {c['n_chars']:5d} "
              f"[{c['section'][:26]:26s}] {head}")


if __name__ == "__main__":
    main()
