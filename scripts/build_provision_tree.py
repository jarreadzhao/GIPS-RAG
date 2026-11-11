"""从 PDF 原件重建 GIPS 条款树，产出 docs/条款树-v1.md（M0 产物）。

设计要点：
    1. 只读原件，不做人工维护的映射表——条款树必须能重跑重建，两次结果哈希一致。
    2. 义务等级优先取章节标题（1.A—Requirements 这种写法），标题没标注时退回
       条款正文的 must / should 措辞；两者都取不到标「待核实」，不臆断。
    3. 输出带内容哈希，便于把「同一份原件 → 同一棵条款树」写进验收记录。

用法：
    python scripts/build_provision_tree.py            # 生成并打印哈希
    python scripts/build_provision_tree.py --check     # 只校验，不写文件
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from app.parse.pdf_text import extract

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "standard_paper" / "2020_gips_standards_firms.pdf"
OUT = ROOT / "docs" / "条款树-v1.md"
EFFECTIVE = "2020-01-01"

SEC_RE = re.compile(r"^\s*(\d{1,2}\.[A-Z])\.\s+(.+)$")
SEC_LABEL_RE = re.compile(r"^(.*?)\s*[—–-]+\s*(Requirements|Recommendations)\s*$")
PROV_LINE_RE = re.compile(r"^\s*(\d{1,2}\.[A-Z]\.\d{1,3})(?:\s+(.*))?$")
NOISE_RE = re.compile(r"© 2019 CFA Institute|www\.cfainstitute\.org|^\d+\s*\|\s*$|GIPS®? for Firms")
# 章节标题折行的判定：无句末标点，且要么以小写词开头（承接上一行），
# 要么本身很短（≤4 个词）。这样 "Guidelines" 能并入标题，
# 而 "The firm must disclose the ..." 这类正文句不会被误并。
TITLE_TAIL_END = tuple(".!?:;")


def _is_title_tail(line: str) -> bool:
    if not line or line.endswith(TITLE_TAIL_END):
        return False
    return line[0].islower() or len(line.split()) <= 4


def build() -> dict:
    parsed = extract(str(PDF))
    pages = parsed.pages_text

    chapters: list[dict] = []
    for txt in pages:
        if "CONTENTS" in txt:
            for line in txt.splitlines():
                m = re.match(r"^(\d{1,2})\.\s+([A-Za-z][^\t]+)$", line.strip())
                if m:
                    chapters.append({"no": int(m.group(1)), "title": m.group(2).strip()})
            break

    sections: list[dict] = []
    provisions: list[dict] = []
    cur_sec = cur_prov = pending_sec = None
    pending_left = 0

    def close_prov() -> None:
        nonlocal cur_prov
        if not cur_prov:
            return
        body = " ".join(cur_prov.pop("lines")).strip()
        obligations = {
            "REQUIREMENT": bool(re.search(r"\bmust\b", body, re.I)),
            "RECOMMENDATION": bool(re.search(r"\bshould\b", body, re.I)),
        }
        cur_prov["obligation"] = (
            "REQUIREMENT" if obligations["REQUIREMENT"]
            else "RECOMMENDATION" if obligations["RECOMMENDATION"] else "待核实"
        )
        cur_prov["lead"] = " ".join(body.split())[:70]
        provisions.append(cur_prov)
        cur_prov = None

    for page_no, txt in enumerate(pages, start=1):
        for raw in txt.splitlines():
            line = raw.strip()
            if not line:
                continue
            m = SEC_RE.match(line)
            if m and not PROV_LINE_RE.match(line):
                close_prov()
                label = SEC_LABEL_RE.match(m.group(2))
                sec = {
                    "id": m.group(1),
                    "title": label.group(1) if label else m.group(2),
                    "label": label.group(2) if label else None,
                    "page": page_no,
                    "provisions": [],
                }
                sections.append(sec)
                cur_sec = sec
                # 章节标题可能折行，只允许补一行，避免把条款正文并进标题
                pending_sec, pending_left = (None, 0) if label else (sec, 1)
                continue
            if pending_sec is not None and pending_left > 0:
                if (_is_title_tail(line) and not PROV_LINE_RE.match(line)
                        and not NOISE_RE.search(line)):
                    pending_sec["title"] = (pending_sec["title"] + " " + line).strip()
                    # 已并入标题的行不再参与后续解析
                    pending_sec, pending_left = None, 0
                    continue
                # 不像标题续行就停止尝试，本行继续按条款/正文处理
                pending_sec, pending_left = None, 0
            p = PROV_LINE_RE.match(line)
            if p:
                close_prov()
                cur_prov = {
                    "id": p.group(1), "page": page_no,
                    "section": cur_sec["id"] if cur_sec else None,
                    "lines": [p.group(2) or ""],
                }
                if cur_sec:
                    cur_sec["provisions"].append(p.group(1))
                continue
            if cur_prov is not None and not NOISE_RE.search(line):
                cur_prov["lines"].append(line)
    close_prov()

    sec_obl: dict[str, str] = {}
    for s in sections:
        title = " ".join(s["title"].split()).replace("\x07", "")
        if s["label"]:
            sec_obl[s["id"]] = "REQUIREMENT" if s["label"].startswith("Requirement") else "RECOMMENDATION"
        elif "Requirements" in title:
            sec_obl[s["id"]] = "REQUIREMENT"
        elif "Recommendations" in title:
            sec_obl[s["id"]] = "RECOMMENDATION"

    seen: set[str] = set()
    uniq: list[dict] = []
    for p in provisions:
        if p["id"] in seen:      # 同一编号在原件里重复出现时只留首次
            continue
        seen.add(p["id"])
        p["obligation"] = sec_obl.get(p["section"]) or p["obligation"]
        uniq.append(p)

    # 节标题上的义务等级：优先用章节标注；没有标注时按节内条款实际等级汇总，
    # 避免出现「节标题写着待核实、节内条款却是 REQUIREMENT」的矛盾展示。
    sec_obl_display: dict[str, str] = {}
    for s in sections:
        if s["id"] in sec_obl:
            sec_obl_display[s["id"]] = sec_obl[s["id"]]
            continue
        kinds = {p["obligation"] for p in uniq if p["section"] == s["id"]}
        if not kinds:
            continue
        sec_obl_display[s["id"]] = kinds.pop() if len(kinds) == 1 else "混合"

    return {
        "source": PDF.name,
        "effective_date": EFFECTIVE,
        "pages": len(pages),
        "chapters": chapters,
        "sections": [
            {
                "id": s["id"],
                "title": re.sub(r"\s*[—–-]+\s*(Requirements|Recommendations)\s*$", "",
                               " ".join(s["title"].split()).replace("\x07", "")),
                "obligation": sec_obl_display.get(s["id"]),
                "page": s["page"], "n": len(s["provisions"]),
            }
            for s in sections
        ],
        "provisions": uniq,
    }


def render(tree: dict) -> str:
    provs = tree["provisions"]
    stat = {k: sum(1 for p in provs if p["obligation"] == k)
            for k in ("REQUIREMENT", "RECOMMENDATION", "待核实")}
    L = [
        "# GIPS 条款树 v1（2020 版 · Firms）",
        "",
        "| 项目 | 内容 |",
        "|---|---|",
        f"| 来源原件 | `standard_paper/{tree['source']}`（{tree['pages']} 页） |",
        f"| 生效期 | {tree['effective_date']}（原件 Effective Date 节：2020 版的生效日为 1 January 2020） |",
        f"| 结构 | {len(tree['chapters'])} 章 / {len(tree['sections'])} 节 / {len(provs)} 条 Provision |",
        f"| 义务等级 | REQUIREMENT {stat['REQUIREMENT']} 条；RECOMMENDATION {stat['RECOMMENDATION']} 条；"
        f"待核实 {stat['待核实']} 条 |",
        "| 重建方式 | `python scripts/build_provision_tree.py`（只读原件，可重复重建，内容哈希见文末） |",
        "",
        "> 义务等级的判定顺序：① 章节标题标注（如 `1.A. Fundamentals of Compliance—Requirements`）；"
        "② 条款正文措辞（`must`/`should`）；③ 两者都没有则标「待核实」，不做推断。"
        "不标 `—Requirements/—Recommendations` 的章节（第 8 章广告指引）按 ② 判定。",
        "",
        "## 章节目录",
        "",
        "| 章 | 标题 | 节 | 条款数 |",
        "|---|---|---|---|",
    ]
    for ch in tree["chapters"]:
        secs = [s for s in tree["sections"] if int(s["id"].split(".")[0]) == ch["no"]]
        L.append(f"| {ch['no']} | {ch['title']} | "
                 f"{' / '.join(s['id'] for s in secs) or '—'} | {sum(s['n'] for s in secs)} |")

    L += ["", "## 条款明细", ""]
    for ch in tree["chapters"]:
        secs = [s for s in tree["sections"] if int(s["id"].split(".")[0]) == ch["no"]]
        if not secs:
            continue
        L += [f"### 第 {ch['no']} 章 {ch['title']}", ""]
        for s in secs:
            obl = s["obligation"] or "待核实"
            L += [f"#### {s['id']} {s['title']}（{obl}，原件 p{s['page']}，{s['n']} 条）", "",
                  "| 编号 | 义务等级 | 页码 | 首句（截断 70 字符） |", "|---|---|---|---|"]
            for p in (p for p in provs if p["section"] == s["id"]):
                lead = p["lead"].replace("|", "\\|")
                L.append(f"| {p['id']} | {p['obligation']} | p{p['page']} | {lead} |")
            L.append("")

    L += ["---", "",
          "## 重建哈希", "",
          "对上面全部条款记录（编号、页码、义务等级、首句）按编号排序后取 sha256，"
          "用于验证「同一原件重建两次结果一致」：", ""]
    return "\n".join(L)


def digest(tree: dict) -> str:
    rows = sorted((p["id"], p["page"], p["obligation"], p["lead"]) for p in tree["provisions"])
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只计算哈希，不写文件")
    a = ap.parse_args()
    tree = build()
    h = digest(tree)
    text = render(tree) + f"\n```\nsha256 = {h}\n```\n"
    if not a.check:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(text, encoding="utf-8")
    print(f"条款 {len(tree['provisions'])} 条，{len(tree['sections'])} 节，sha256 = {h}")
    print("写入", OUT if not a.check else "（未写入）")


if __name__ == "__main__":
    main()
