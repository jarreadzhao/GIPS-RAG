# -*- coding: utf-8 -*-
"""体检：中文版章节覆盖、正文中文占比、段落配比失配情况"""
import io, re, os

D = os.environ.get("GIPS_PAPERS_DIR", "work/papers")
zh = io.open(os.path.join(D, "_ZH.txt"), encoding="utf-8").read()
en = io.open(os.path.join(D, "_EN.txt"), encoding="utf-8").read()

SEC = ['1','2','3','3.1','3.2','3.2.1','3.2.2','3.2.3','3.3','3.4','3.5',
       '4','5','5.1','5.2','5.3','5.4','6','6.1','6.2','6.3','7']

def split_sections(t, label):
    hits = []
    for num in SEC:
        for m in re.finditer(r'(?m)^' + re.escape(num) + r'[ \t]+(\S.*)$', t):
            ti = m.group(1).strip()
            hits.append((m.start(), num, ti))
    hits.sort()
    out = []
    for i, (s, n, ti) in enumerate(hits):
        e = hits[i+1][0] if i+1 < len(hits) else len(t)
        out.append((n, ti, t[s:e]))
    print(f"=== {label} 命中 {len(out)} 个章节标题 ===")
    for n, ti, _ in out:
        print(f"   {n:6s} {ti[:44]}")
    return out

en_s = split_sections(en, "EN")
print()
zh_s = split_sections(zh, "ZH")

print()
print("=== 章节覆盖差异 ===")
ne = [n for n, _, _ in en_s]
nz = [n for n, _, _ in zh_s]
print("  EN 有 ZH 无:", [x for x in ne if x not in nz])
print("  ZH 有 EN 无:", [x for x in nz if x not in ne])

print()
print("=== 每节段落数配比（n_en vs n_zh，差异越大越靠比例分配凑）===")
def to_paras(body):
    ps = re.split(r"\n\s*\n", body)
    return [re.sub(r"\s*\n\s*", " ", p).strip() for p in ps if p.strip()]

for n in nz:
    be = next((b for m, _, b in en_s if m == n), None)
    bz = next((b for m, _, b in zh_s if m == n), None)
    if be is None or bz is None:
        continue
    pe, pz = to_paras(be), to_paras(bz)
    flag = "  <== 失配" if abs(len(pe) - len(pz)) > max(2, 0.3 * len(pe)) else ""
    print(f"   {n:6s} EN 段 {len(pe):3d} | ZH 段 {len(pz):3d}{flag}")

print()
print("=== 正文中文占比（截至 References 之前）===")
cut = zh.lower().find("references")
body = zh[:cut] if cut > 0 else zh
zhc = sum(1 for c in body if '\u4e00' <= c <= '\u9fff')
print(f"  正文长度 {len(body)}  中文字符 {zhc}  占比 {zhc/max(len(body),1):.1%}")
print(f"  <EOS>/<pad> 等噪声标记：", len(re.findall(r'<\s*(?:EOS|pad|endoftext|unk|/s|/e)\s*>', zh, re.I)))
