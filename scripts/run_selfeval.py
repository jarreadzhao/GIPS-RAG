"""自生成口径评测执行器（AC-82 / AC-13 / AC-14）。

先用 dataset.load_set 校验目录隔离与元数据，再用 HTTP 打真实检索服务，
同配置连跑两次，输出 Recall@1/5/10、MRR 与两次差值。

不启用 enforce_release_guard：自生成口径没有人工抽检，AC-24A/B/C/D 那套发布
判据属于真实工单口径，已被 PRD 1.3 移出本期；这里只对 AC-82 的稳定性负责。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.eval.dataset import load_set  # noqa: E402
from app.eval.runner import config_hash, ensure_trusted_report, run_evaluation  # noqa: E402


def make_retriever(base_url: str, role: str, dept: str, top_k: int, timeout: float):
    """返回 (query -> 命中的条款编号列表) 的函数，并缓存最后一次原始响应字段。"""
    client = httpx.Client(base_url=base_url, timeout=timeout,
                          headers={"X-Role": role, "X-Dept": dept})
    probe: dict = {}

    def retrieve(query: str) -> list[str]:
        resp = client.post("/retrieval/test", json={
            "query": query, "top_k": top_k,
            "channels": ["dense", "sparse", "sql"], "config": {}})
        resp.raise_for_status()
        body = resp.json()
        probe.setdefault("index_version", body.get("index_version"))
        out: list[str] = []
        for hit in body.get("hits") or []:
            pid = hit.get("provision_id")
            if pid and pid not in out:
                out.append(pid)
        return out

    return retrieve, probe


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="eval/golden/golden-v1.jsonl")
    ap.add_argument("--base-url", default="http://127.0.0.1:8011")
    ap.add_argument("--role", default="admin")
    ap.add_argument("--dept", default="compliance")
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--out", default="var/reports/selfeval.json")
    a = ap.parse_args()

    evalset = load_set(Path(a.set))
    retrieve, probe = make_retriever(a.base_url, a.role, a.dept, a.top_k, a.timeout)

    first = run_evaluation(evalset, retrieve, model_snapshot_id="rule:selfgen-v1",
                           config={"top_k": a.top_k, "caliber": "self_generated"},
                           enforce_release_guard=False)
    config = {"top_k": a.top_k, "channels": "dense,sparse,sql", "rerank": True,
              "index_version": probe.get("index_version"), "caliber": "self_generated"}
    second = run_evaluation(evalset, retrieve, model_snapshot_id="rule:selfgen-v1",
                            config=config, enforce_release_guard=False)

    m1, m2 = first["ai_caliber"], second["ai_caliber"]
    diff = round(abs((m1["recall@5"] or 0) - (m2["recall@5"] or 0)), 4)
    report = {
        "set_id": evalset.set_id,
        "content_hash": evalset.content_hash,
        "item_count": len(evalset.items),
        "config": config,
        "config_hash": config_hash(config),
        "run1": m1, "run2": m2,
        "recall5_diff": diff,
        "ac82_pass": (m1["recall@5"] or 0) >= 0.92 and diff <= 0.01,
    }
    # AC-70：写盘前自检自证——自产报告必须能通过采信守卫，
    # 防止上游改动静默破坏 config_hash 字段后写出一批无法溯源的结果
    ensure_trusted_report(report)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("集合 %s / %d 条 / 内容哈希 %s" % (evalset.set_id, len(evalset.items),
                                            evalset.content_hash))
    print("index_version=%s" % config["index_version"])
    for k in ("recall@1", "recall@5", "recall@10", "mrr"):
        print("%-10s 第一次 %-8s 第二次 %-8s" % (k, m1[k], m2[k]))
    print("Recall@5 差值 %.4f（AC-14/82 阈值 ≤0.01）" % diff)
    print("AC-82 判据（R@5 ≥0.92 且 差值 ≤0.01）：%s" % ("通过" if report["ac82_pass"] else "未通过"))
    print("结果写入 %s" % out)
    return 0 if report["ac82_pass"] else 2


if __name__ == "__main__":
    sys.exit(main())
