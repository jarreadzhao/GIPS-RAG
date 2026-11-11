# -*- coding: utf-8 -*-
"""体检中文版覆盖度与对齐质量"""
import io, re, os

D = os.environ.get("GIPS_PAPERS_DIR", "work/papers")
zh = io.open(os.path.join(D, "_ZH.txt"), encoding="utf-8").read()
en = io.open(os.path.join(D, "_EN.txt"), encoding="utf-8").read()

print("=== 关键词覆盖 ===")
for kw in ["结果", "机器翻译", "模型变体", "结论", "优化器", "正则化", "英语成分句法分析"]:
    print(f"  {kw:12s} ZH 出现 {zh.count(kw):3d} 次   EN 对应词出现 {en.lower().count({'结果':'result','机器翻译':'machine translation','模型变体':'model variation','结论':'conclusion','优化器':'optimizer','正则化':'regulariz','英语成分句法分析':'constituency'}[kw]):3d} 次")

print()
print("=== _ZH.txt 最后 25 行非空 ===")
ne = [l.strip() for l in zh.split("\n") if l.strip()]
for l in ne[-25:]:
    print("  ", l[:86])

print()
print("=== _ZH.txt 行数 / 段落数 ===")
print("  非空行", len(ne))
paras_zh = [p for p in re.split(r"\n\s*\n", zh) if p.strip()]
print("  空行分段", len(paras_zh))
