"""抽取中文单栏 PDF 的段落。

与 extract.py（英文双栏）的差别：
1. 中文版是单栏（x0 恒定、块宽约占版心 65%），无需分栏判定；
2. 中文版是机器翻译版，页面里混有大量噪声块：注意力可视化图把每个单词
   单独排成一块，并夹带 <EOS>/<pad> 等训练 token；这些必须丢掉，
   否则它们会以"短文本块"的形式占满 chunk 预算，且向量毫无信息量；
3. 中文标题形如 "3.2\\n注意力"（编号与标题在同一块内、换行分隔），
   归一化后按编号切章节。
"""
import json
import re
import sys
from pathlib import Path

import pymupdf

NOISE_TOK = re.compile(r"^<\s*(?:EOS|pad|endoftext|unk|/s|/e)\s*>$", re.I)
PAGE_NO = re.compile(r"^\d{1,3}$")
CJK = re.compile(r"[\u4e00-\u9fff]")
# 单块内几乎全是孤立英文单词（注意力可视化图）判据：平均每行字符数 < 6 且无中文
FIGURE_DUMP = re.compile(r"^Input-Input|^Input-Input Layer5", re.I)


def norm(t: str) -> str:
    t = t.replace("\u00ad", "")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\s*\n\s*", " ", t)
    return t.strip()


def is_heading(t: str):
    m = re.match(r"^(\d+(?:\.\d+)*)\s+(\S.*)$", t)
    if m and len(t) <= 40:
        return f"{m.group(1)} {m.group(2)}"
    if t in ("References", "参考文献", "Abstract", "摘要", "致谢"):
        return t
    return None


def looks_like_word_dump(raw: str) -> bool:
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    if len(lines) < 3:
        return False
    avg = sum(len(l) for l in lines) / len(lines)
    return avg < 7 and not CJK.search(raw)


def extract(pdf: str):
    doc = pymupdf.open(pdf)
    paras, dropped, in_refs = [], 0, False
    for pno, page in enumerate(doc, 1):
        blocks = page.get_text("blocks")
        blocks.sort(key=lambda b: (round(b[1], 1), b[0]))
        for b in blocks:
            raw = b[4]
            if looks_like_word_dump(raw) or FIGURE_DUMP.match(raw.strip()):
                dropped += 1
                continue
            t = norm(raw)
            if not t or PAGE_NO.match(t) or NOISE_TOK.match(t):
                dropped += 1
                continue
            if len(t) < 2:
                dropped += 1
                continue
            h = is_heading(t)
            if h in ("References", "参考文献"):
                in_refs = True
                continue
            if in_refs:
                continue
            paras.append({"page": pno, "text": t, "is_heading": bool(h), "heading": h,
                          "n_chars": len(t)})
    doc.close()
    return paras, dropped


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else \
        "work/Attention-Is-All-You-Need-ZH.pdf"
    out = sys.argv[2] if len(sys.argv) > 2 else "work/paras_zh.jsonl"
    paras, dropped = extract(src)
    with open(out, "w", encoding="utf-8") as f:
        for p in paras:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    body = [p for p in paras if not p["is_heading"]]
    zh = sum(1 for p in body for c in p["text"] if "\u4e00" <= c <= "\u9fff")
    tot = sum(len(p["text"]) for p in body)
    print(f"段落 {len(paras)}（正文 {len(body)}） | 丢弃噪声块 {dropped}")
    print(f"正文长度 {tot}  中文字符 {zh}  占比 {zh / max(tot, 1):.1%}")
    print(f"段落长度 min/中位/max = {min(p['n_chars'] for p in body)} / "
          f"{sorted(p['n_chars'] for p in body)[len(body) // 2]} / "
          f"{max(p['n_chars'] for p in body)}")
    print("\n章节标题：")
    for p in paras:
        if p["is_heading"] and p["heading"]:
            print(f"  p{p['page']:2d} {p['heading']}")


if __name__ == "__main__":
    main()
