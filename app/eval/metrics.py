"""检索指标：Recall@1/5/10、MRR 与双口径校验（AC-13/14/24D）。"""
from __future__ import annotations

from dataclasses import dataclass

from app.eval.dataset import MAX_CALIBER_DIFF, EvalGuardError

K_VALUES = (1, 5, 10)


@dataclass
class MetricRow:
    """一条查询的标注与检索结果（按名次排列的条款编号）。"""

    item_id: str
    gold: list[str]
    predicted: list[str]


def recall_at(predicted: list[str], gold: list[str], k: int) -> float:
    """Top-K 中命中任一 gold 条款记 1，否则 0。"""
    if not gold:
        return 0.0
    return 1.0 if set(predicted[:k]) & set(gold) else 0.0


def reciprocal_rank(predicted: list[str], gold: list[str]) -> float:
    """第一个命中的倒数名次。"""
    wanted = set(gold)
    for rank, pid in enumerate(predicted, start=1):
        if pid in wanted:
            return 1.0 / rank
    return 0.0


def summarize(rows: list[MetricRow]) -> dict:
    """汇总一个口径下的指标；空集合返回 N/A 而不是 0。"""
    if not rows:
        return {f"recall@{k}": None for k in K_VALUES} | {"mrr": None, "n": 0}
    n = len(rows)
    out = {f"recall@{k}": round(sum(recall_at(r.predicted, r.gold, k) for r in rows) / n, 4)
           for k in K_VALUES}
    out["mrr"] = round(sum(reciprocal_rank(r.predicted, r.gold) for r in rows) / n, 4)
    out["n"] = n
    return out


def check_dual_caliber(all_metrics: dict, human_metrics: dict) -> float:
    """AC-24D：两种口径的 Recall@5 差值不得大于 0.03。"""
    if human_metrics.get("recall@5") is None or all_metrics.get("recall@5") is None:
        raise EvalGuardError("缺少某一口径的 Recall@5，无法出具双口径报告")
    diff = abs(all_metrics["recall@5"] - human_metrics["recall@5"])
    if diff > MAX_CALIBER_DIFF:
        raise EvalGuardError(
            f"AI 全量口径与人工抽检口径 Recall@5 相差 {diff:.3f} > {MAX_CALIBER_DIFF}，"
            "报告不合格，需复核标注")
    return round(diff, 4)


def check_stability(first: dict, second: dict, max_diff: float = 0.01) -> float:
    """AC-14：同配置连跑两次，Recall@5 差值不得超过 1 个百分点。"""
    diff = abs((first.get("recall@5") or 0.0) - (second.get("recall@5") or 0.0))
    if diff > max_diff:
        raise EvalGuardError(f"两次 Recall@5 相差 {diff:.4f} > {max_diff}，结果不稳定")
    return round(diff, 4)
