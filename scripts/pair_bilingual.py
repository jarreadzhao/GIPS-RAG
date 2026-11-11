"""给英文原文与中文译版的 chunk 写 pair_id。

用法：
    python scripts/pair_bilingual.py --db var/gips.db \
        --source gips2020 --target gips2020-zh --out var/reports/bilingual_pairs.json

只改 `chunks.pair_id`，不动文本、页码与索引；重复执行结果一致。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.db import Database  # noqa: E402
from app.index.pairing import assign_pair_ids  # noqa: E402

COLUMNS = ("chunk_id, provision_id, page_start, char_start, lang, status")


def load_chunks(db: Database, doc_id: str, lang: str) -> list[dict]:
    rows = db.fetch_all(
        f"SELECT {COLUMNS} FROM chunks WHERE doc_id=? AND lang=? "
        "AND status='active' AND provision_id IS NOT NULL "
        "ORDER BY page_start, char_start",
        (doc_id, lang),
    )
    return [dict(r) for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="var/gips.db")
    parser.add_argument("--source", default="gips2020")
    parser.add_argument("--target", default="gips2020-zh")
    parser.add_argument("--out", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db = Database(Path(args.db))
    en = load_chunks(db, args.source, "en")
    zh = load_chunks(db, args.target, "zh")
    if not zh:
        print(f"目标文档 {args.target} 没有可配对的 zh chunk")
        return 2

    mapping = assign_pair_ids(en, zh)
    en_ids = {c["chunk_id"] for c in en}
    zh_ids = {c["chunk_id"] for c in zh}
    exact = sum(1 for chunk_id, pair_id in mapping.items()
                if chunk_id in en_ids and pair_id.count(":") == 2)
    provision_level = sum(1 for chunk_id, pair_id in mapping.items()
                          if chunk_id in en_ids and pair_id.count(":") == 1)
    en_paired = sum(1 for c in en if c["chunk_id"] in mapping)
    zh_paired = sum(1 for c in zh if c["chunk_id"] in mapping)

    summary = {
        "source": args.source,
        "target": args.target,
        "en_chunks": len(en),
        "zh_chunks": len(zh),
        "en_paired": en_paired,
        "zh_paired": zh_paired,
        "en_paired_rate": round(en_paired / len(en), 4) if en else None,
        "pairs_exact_1to1": exact,
        "pairs_provision_level": provision_level,
        "en_provisions": len({c["provision_id"] for c in en}),
        "zh_provisions": len({c["provision_id"] for c in zh}),
        "en_only_provisions": sorted(
            {c["provision_id"] for c in en} - {c["provision_id"] for c in zh}),
        "zh_only_provisions": sorted(
            {c["provision_id"] for c in zh} - {c["provision_id"] for c in en}),
        "dry_run": args.dry_run,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
    if args.dry_run:
        return 0

    def _write(conn) -> None:
        conn.execute("UPDATE chunks SET pair_id=NULL WHERE doc_id IN (?,?)",
                     (args.source, args.target))
        conn.executemany("UPDATE chunks SET pair_id=? WHERE chunk_id=?",
                         [(pair_id, chunk_id) for chunk_id, pair_id in mapping.items()])

    db.run_write(_write)
    print(f"已写入 {len(mapping)} 条 pair_id")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
