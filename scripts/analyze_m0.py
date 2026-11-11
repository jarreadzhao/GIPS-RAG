"""一次性统计脚本：为 M0 的解析质量报告 / 切分对比报告 / 条款树收集真实数据。"""
import json
import re
import statistics
from pathlib import Path

from app.parse.chunker import chunk_document
from app.parse.cleaner import clean
from app.parse.pdf_text import ParsedDocument, extract

ROOT = Path(__file__).resolve().parents[1]
DOCS = [
    ("gips2020_en", ROOT / "standard_paper" / "2020_gips_standards_firms.pdf", "en"),
    ("gips2020_zh", ROOT / "standard_paper" / "Chinese-Translation-2020-Firms.pdf", "zh"),
]

out = {"documents": {}, "strategies": {}, "tree": None}

for key, path, lang in DOCS:
    parsed = extract(str(path), header_margin_pt=60, footer_margin_pt=48)
    cleaned = clean(parsed)
    doc = ParsedDocument(pages_text=cleaned.pages_text, blocks=cleaned.blocks,
                         report=parsed.report)
    out["documents"][key] = {
        "file": path.name,
        "pages": len(parsed.pages_text),
        "coverage": round(parsed.report.coverage, 4),
        "text_pages": len(parsed.report.text_pages),
        "scanned_pages": len(parsed.report.scanned_pages),
        "blocks_raw": len(parsed.blocks),
        "blocks_clean": len(cleaned.blocks),
        "dropped": dict(cleaned.dropped),
        "stopped_at_page": cleaned.stopped_at_page,
    }

    per = {}
    for strat in ("provision", "section", "paragraph", "semantic"):
        chunks = chunk_document(doc, strategy=strat, lang=lang)
        n = len(chunks)
        sizes = [len(c.text) for c in chunks]
        empty = sum(1 for c in chunks if not c.text.strip())
        anchors = span_ok = over = 0
        by_page: dict[int, list] = {}
        for c in chunks:
            page = cleaned.pages_text[c.page_no - 1]
            if c.text.strip() and c.text.strip() in page:
                anchors += 1
            if c.text == page[c.char_start:c.char_end]:
                span_ok += 1
            if len(c.text) > 1100:
                over += 1
            by_page.setdefault(c.page_no, []).append(c)
        overlap = 0
        for cs in by_page.values():
            cs.sort(key=lambda x: x.char_start)
            for a, b in zip(cs, cs[1:]):
                if b.char_start < a.char_end:
                    overlap += 1
        per[strat] = {
            "n": n,
            "chars_min": min(sizes) if sizes else 0,
            "chars_median": int(statistics.median(sizes)) if sizes else 0,
            "chars_max": max(sizes) if sizes else 0,
            "empty": empty,
            "anchor_ok": anchors,
            "anchor_rate": round(anchors / n, 4) if n else 0,
            "span_exact": span_ok,
            "span_rate": round(span_ok / n, 4) if n else 0,
            "overlap": overlap,
            "over_max": over,
            "with_provision": sum(1 for c in chunks if c.provision_id),
        }
    out["strategies"][key] = per

    if key == "gips2020_en":
        sections, provs = [], []
        for page_no, txt in enumerate(parsed.pages_text, start=1):
            for line in txt.splitlines():
                m = re.match(
                    r"^\s*(\d{1,2}\.[A-Z])\.\s+(.+?)\s*[—–-]+\s*(Requirements|Recommendations)\s*$",
                    line,
                )
                if m:
                    sections.append({
                        "id": m.group(1), "title": m.group(2).strip(),
                        "obligation": m.group(3), "page": page_no, "provisions": [],
                    })
            for m in re.finditer(r"^[ \t]*(\d{1,2}\.[A-Z]\.\d{1,3})[ \t]*$", txt, re.M):
                provs.append({"id": m.group(1), "page": page_no})

        by_id = {s["id"]: s for s in sections}
        orphan = []
        for p in provs:
            sec = p["id"].rsplit(".", 1)[0]
            if sec in by_id:
                by_id[sec]["provisions"].append(p["id"])
            else:
                orphan.append(p)

        chapters = []
        for txt in parsed.pages_text:
            if "CONTENTS" in txt:
                for line in txt.splitlines():
                    m = re.match(r"^(\d{1,2})\.\s+([A-Za-z][^\t]+)$", line.strip())
                    if m:
                        chapters.append({"no": int(m.group(1)), "title": m.group(2).strip()})
        out["tree"] = {
            "chapters": chapters, "sections": sections,
            "provision_total": len(provs), "orphans": orphan,
        }

(ROOT / "var" / "m0_analysis.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print("documents:", json.dumps(out["documents"], ensure_ascii=False))
print("strategies:", json.dumps(out["strategies"], ensure_ascii=False))
t = out["tree"]
print("chapters:", len(t["chapters"]), t["chapters"][:3])
print("sections:", len(t["sections"]), "provisions:", t["provision_total"], "orphans:", len(t["orphans"]))
print("section head sample:", t["sections"][:4])
