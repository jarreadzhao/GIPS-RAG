"""把 pair 压成"双语成对"的检索单元。

关键约束：切分必须以 pair 为单位。
两边各自按字符数切分，会让同一个内容在两套索引里落在不同边界上，
返回的"对照"就变成两段不相干的内容——这种错误不报错，只在被追问时才暴露。
以 pair 为切分原子，en/zh 天然同进同出。

输出两份语料，供同一批查询做对照实验：
  work/pc_en.jsonl  text = 英文侧（gold 也按英文侧判定）
  work/pc_zh.jsonl  text = 中文侧（gold 仍按 en_text 判定）
这样两组的召回指标可直接比较，差值即"用中文版当索引"的净效果。
"""
import json
from pathlib import Path

TARGET_EN = 550     # 目标英文侧字符数（对应中文侧约 300 字）
MAX_EN = 1100       # 上限，超过则强制断开


def build(pairs):
    chunks = []
    buf, cur = [], 0

    def flush():
        nonlocal buf, cur
        if not buf:
            return
        en = "\n".join(p["en"] for p in buf).strip()
        zh = "\n".join(p["zh"] for p in buf if p["zh"]).strip()
        if en:
            chunks.append({
                "chunk_id": f"b{len(chunks) + 1:03d}",
                "section": buf[0]["section"],
                "pair_ids": [p["pair_id"] for p in buf],
                "page_en": min(p["page_en"] for p in buf),
                "page_zh": min(p["page_zh"] for p in buf if p.get("page_zh")) if any(
                    p.get("page_zh") for p in buf) else None,
                "en_text": en,
                "zh_text": zh,
                "n_en": len(en),
                "n_zh": len(zh),
            })
        buf, cur = [], 0

    for p in pairs:
        n = len(p["en"])
        if cur and (cur >= TARGET_EN or cur + n > MAX_EN):
            flush()
        buf.append(p)
        cur += n
    flush()
    return chunks


def main():
    pairs = [json.loads(l) for l in open("work/pairs.jsonl", encoding="utf-8")]
    chunks = build(pairs)

    full = Path("work/paired_chunks.jsonl")
    with open(full, "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    for name, field in (("pc_en.jsonl", "en_text"), ("pc_zh.jsonl", "zh_text")):
        with open(Path("work") / name, "w", encoding="utf-8") as f:
            for c in chunks:
                d = dict(c)
                d["text"] = c[field]
                f.write(json.dumps(d, ensure_ascii=False) + "\n")

    L = [(c["n_en"], c["n_zh"]) for c in chunks]
    print(f"pair {len(pairs)} -> 双语 chunk {len(chunks)}")
    print(f"英文侧 字符 min/中位/max = {min(x[0] for x in L)} / "
          f"{sorted(x[0] for x in L)[len(L) // 2]} / {max(x[0] for x in L)}")
    print(f"中文侧 字符 min/中位/max = {min(x[1] for x in L)} / "
          f"{sorted(x[1] for x in L)[len(L) // 2]} / {max(x[1] for x in L)}")
    empty = [c["chunk_id"] for c in chunks if not c["zh_text"]]
    print(f"缺中文侧的 chunk：{empty or '无'}")
    print("\n前 6 个 chunk：")
    for c in chunks[:6]:
        print(f"  {c['chunk_id']} p{c['page_en']} [{c['section'][:20]:20s}] "
              f"en{c['n_en']:4d} zh{c['n_zh']:4d} pairs={len(c['pair_ids'])}")
        print(f"     EN: {c['en_text'][:64]}")
        print(f"     ZH: {c['zh_text'][:40]}")


if __name__ == "__main__":
    main()
