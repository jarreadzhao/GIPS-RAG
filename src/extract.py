# -*- coding: utf-8 -*-
"""步骤 1：从 PDF 提取段落级文本，保留页码与坐标。

用于纯文本层 PDF（本机这批论文都有文本层，无需 OCR）。
输出 work/paras.jsonl，每行一个段落（PDF 的 block），带 page / bbox / 字符数。
"""
import json
import os
import re
import sys

import fitz

# 页眉页脚安全带（pt）：页面上下各留出的边距，落在此区间的块视为页眉页脚
HEADER_PT = 60
FOOTER_PT = 48

# 判定为噪声的整块（arXiv 授权声明、页码、水印）
NOISE_PATTERNS = [
    re.compile(r'^Provided proper attribution', re.I),
    re.compile(r'^arXiv:\S+'),
    re.compile(r'^\s*\d{1,3}\s*$'),          # 纯页码
    re.compile(r'^(Downloaded|Preprint)', re.I),
]


def norm(text: str) -> str:
    """统一空白，修复跨行断词（neu-\\nral -> neural）。"""
    text = text.replace('\u00a0', ' ')
    text = re.sub(r'-\s*\n\s*', '', text)     # 连字符换行合并
    text = re.sub(r'\s*\n\s*', ' ', text)     # 其余换行压成空格
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()


def is_noise(text: str) -> bool:
    return any(p.search(text) for p in NOISE_PATTERNS) or len(text) < 2


# 章节标题：1 / 3.2 / 3.2.1 开头，后接大写词
HEADING_RE = re.compile(r'^\d{1,2}(\.\d{1,2}){0,2}\s+[A-Z]')
KNOWN_HEADS = {'Abstract', 'References', 'Conclusion', 'Introduction', 'Background'}


def is_heading(text: str) -> bool:
    return bool(HEADING_RE.match(text)) or text in KNOWN_HEADS


def is_label(text: str) -> bool:
    """图表内的单词级标签（注意力可视化页有大量此类块）。

    判据：词数少且不是标题。这类块进索引只会制造伪命中。
    """
    return len(text.split()) < 8 and not is_heading(text)


def detect_columns(blocks, page_w: float) -> int:
    """按块中心 x 的分布判定 1 栏还是 2 栏。

    判据：多数块的宽度明显小于版心宽度时，才可能是多栏；再看中心 x 是否双峰。
    这个判据比"看有没有两个 x 簇"稳，因为单栏里的短行也会形成簇。
    """
    if not blocks:
        return 1
    xs0 = min(b[0] for b in blocks)
    xs1 = max(b[2] for b in blocks)
    text_w = max(xs1 - xs0, 1)
    wide = sum(1 for b in blocks if (b[2] - b[0]) > 0.62 * text_w)
    if wide / len(blocks) >= 0.5:
        return 1
    mid = (xs0 + xs1) / 2
    left = sum(1 for b in blocks if (b[0] + b[2]) / 2 < mid)
    right = len(blocks) - left
    return 2 if left >= 3 and right >= 3 else 1


def extract(pdf_path: str) -> list:
    doc = fitz.open(pdf_path)
    paras = []
    for pno, page in enumerate(doc, start=1):
        H = page.rect.height
        raw = [b for b in page.get_text('blocks') if b[6] == 0]  # 只取文本块
        blocks = []
        for x0, y0, x1, y1, text, _bn, _bt in raw:
            if y0 < HEADER_PT or y1 > H - FOOTER_PT:
                continue
            t = norm(text)
            if is_noise(t) or is_label(t):
                continue
            blocks.append([x0, y0, x1, y1, t])
        if not blocks:
            continue
        ncol = detect_columns(blocks, page.rect.width)
        if ncol == 2:
            xs0 = min(b[0] for b in blocks)
            xs1 = max(b[2] for b in blocks)
            mid = (xs0 + xs1) / 2
            blocks.sort(key=lambda b: (0 if (b[0] + b[2]) / 2 < mid else 1, b[1]))
        else:
            blocks.sort(key=lambda b: b[1])
        for x0, y0, x1, y1, t in blocks:
            paras.append({
                'para_id': len(paras),
                'page': pno,
                'x0': round(x0, 1), 'y0': round(y0, 1),
                'x1': round(x1, 1), 'y1': round(y1, 1),
                'ncol': ncol,
                'text': t,
                'n_chars': len(t),
            })
    doc.close()
    return paras


def main():
    pdf = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else 'work'
    os.makedirs(out_dir, exist_ok=True)
    paras = extract(pdf)
    out = os.path.join(out_dir, 'paras.jsonl')
    with open(out, 'w', encoding='utf-8') as f:
        for p in paras:
            f.write(json.dumps(p, ensure_ascii=False) + '\n')
    total = sum(p['n_chars'] for p in paras)
    multi = sum(1 for p in paras if p['ncol'] == 2)
    print(f'段落 {len(paras)} 条，字符 {total}，双栏页段落 {multi}')
    print(f'页码范围 1..{max(p["page"] for p in paras)}，均 {total//max(len(paras),1)} 字/段')
    print(f'已写出 {out}')


if __name__ == '__main__':
    main()
