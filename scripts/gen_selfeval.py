"""自生成查询集生成器（AC-82 / M1 进入条件）。

口径：用条款自身的编号与义务首句反查该条款，gold 由构造过程直接给出，
不依赖工单、也不依赖模型标注。产物分目录冻结：

    eval/golden/golden-v1.jsonl   + meta.json（带 frozen_at，只读）
    eval/dev/dev-v1.jsonl         + meta.json（调参用）

目录隔离由 app/eval/dataset.py 强制：两者放同一目录会被 load_set 直接拒绝。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

LABELER_MODEL = "rule:selfgen-v1"
SAMPLE_SIZE = 200
DEV_RATIO = 5          # 每 5 条 1 条进 dev


def natural_key(pid: str):
    """按条款编号自然序排序：1.A.2 在 1.A.10 之前。"""
    return [int(t) if t.isdigit() else t for t in re.split(r"[.\-]", pid)]


def first_sentence(text: str) -> str:
    """取义务首句：到第一个句号为止，去掉原文里的省略号与多余空白。"""
    clean = re.sub(r"\s+", " ", text.replace("…", "").replace("...", "")).strip()
    m = re.match(r"(.+?[.;])\s", clean + " ")
    return (m.group(1) if m else clean[:200]).strip()


def load_provisions(csv_path: Path) -> list[dict]:
    rows: list[dict] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            pid = (row.get("条款编号") or "").strip()
            text = (row.get("要求摘要（原文）") or "").strip()
            if not pid or not text:
                continue
            rows.append({
                "provision_id": pid,
                "section": (row.get("章节") or "").strip(),
                "page": (row.get("页码") or "").strip(),
                "obligation": text,
                "quality": (row.get("数据质量") or "").strip(),
            })
    rows.sort(key=lambda r: natural_key(r["provision_id"]))
    return rows


def pick(rows: list[dict], n: int) -> list[dict]:
    """等距抽样，保证 485 条里每个章节都有代表，且结果可复算。"""
    if len(rows) <= n:
        return rows
    return [rows[round(i * (len(rows) - 1) / (n - 1))] for i in range(n)]


def build_items(rows: list[dict]) -> list[dict]:
    """一半按编号查，一半按义务首句查；标题变体单列，不进 Recall@5。"""
    items = []
    for idx, row in enumerate(rows):
        variant = "id" if idx % 2 == 0 else "obligation"
        query = row["provision_id"] if variant == "id" else first_sentence(row["obligation"])
        items.append({
            "item_id": "sg-%04d" % (idx + 1),
            "query": query,
            "gold_provisions": [row["provision_id"]],
            "variant": variant,
            "page": row["page"],
            "labeler_model": LABELER_MODEL,
            "labeler_prompt_hash": "",
            "confidence": 1.0,
            "self_consistency": 1.0,
            "is_table_provision": False,
            "human_reviewed": False,
            "source": "self_generated",
        })
    return items


def is_dev(item_id: str) -> bool:
    return hashlib.sha256(item_id.encode("utf-8")).digest()[0] % DEV_RATIO == 0


def write_set(items: list[dict], path: Path, set_id: str, script_hash: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for item in items:
            item = dict(item)
            item["labeler_prompt_hash"] = script_hash
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    meta = {
        "set_id": set_id,
        "version": "1",
        "item_count": len(items),
        "frozen_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": "scripts/gen_selfeval.py",
        "generator_hash": script_hash,
        "caliber": "self_generated",
    }
    (path.parent / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="reports/条款证据清单.csv")
    ap.add_argument("--root", default=".")
    ap.add_argument("--size", type=int, default=SAMPLE_SIZE)
    a = ap.parse_args()

    root = Path(a.root)
    rows = load_provisions(root / a.csv)
    if len(rows) < a.size:
        raise SystemExit("可用条款只有 %d 条，不足 %d" % (len(rows), a.size))

    sampled = pick(rows, a.size)
    items = build_items(sampled)
    golden = [i for i in items if not is_dev(i["item_id"])]
    dev = [i for i in items if is_dev(i["item_id"])]

    script_hash = "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    write_set(golden, root / "eval/golden/golden-v1.jsonl", "selfgen-golden-v1", script_hash)
    write_set(dev, root / "eval/dev/dev-v1.jsonl", "selfgen-dev-v1", script_hash)

    print("条款总数 %d，抽样 %d" % (len(rows), len(sampled)))
    print("golden %d 条 / dev %d 条" % (len(golden), len(dev)))
    print("生成器哈希 %s" % script_hash)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
