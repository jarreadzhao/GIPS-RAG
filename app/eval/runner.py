"""评测执行与发布判据编排（AC-13 / 14 / 24A-D）。

顺序不能调：先校验评测集元数据，再跑检索，最后才谈指标。
否则很容易出现「指标很漂亮，但评测集根本没冻结」的假通过。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Callable

from app.eval.dataset import (EvalGuardError, EvalSet, check_contamination,
                              check_release_ready, check_sampling,
                              check_self_consistency, record_contact)
from app.eval.metrics import (MetricRow, check_dual_caliber, check_stability, summarize)

RetrieveFn = Callable[[str], list[str]]

# config_hash 固定格式：sha256: + 64 位小写十六进制
CONFIG_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}")


def config_hash(config: dict) -> str:
    """配置哈希：缺它结果不得作为发布依据。"""
    raw = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ensure_trusted_report(report: dict) -> None:
    """AC-70：评测结果采信守卫。

    缺 config_hash 或格式不是 `sha256:<64 位十六进制>` 的报告一律不采信：
    没有配置哈希就无法回答「这组数字是在什么配置下跑出来的」，历史对比
    与发布判据都会失去可比性。message 必须点明 config_hash 与拒绝原因。
    """
    if not isinstance(report, dict):
        raise EvalGuardError(
            f"评测结果不是 JSON 对象（实际为 {type(report).__name__}），"
            "缺少 config_hash，不予采信")
    value = report.get("config_hash")
    if not isinstance(value, str) or not CONFIG_HASH_RE.fullmatch(value):
        raise EvalGuardError(
            "评测结果的 config_hash 缺失或格式非法"
            f"（期望 sha256: + 64 位小写十六进制，实际为 {value!r}），不予采信")


def load_report(path: str | Path) -> dict:
    """评测结果采信入口：读入报告文件，先过 config_hash 守卫再返回。

    等价于「查询接口不返回不可信结果」：任何下游消费评测报告的路径
    都应经由本函数，而不是直接 json.load。
    """
    path = Path(path)
    if not path.exists():
        raise EvalGuardError(f"评测报告不存在：{path}")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvalGuardError(f"评测报告不是合法 JSON：{exc}") from exc
    ensure_trusted_report(report)
    return report


def _collect(evalset: EvalSet, retrieve_fn: RetrieveFn) -> list[MetricRow]:
    return [MetricRow(item_id=i.item_id, gold=i.gold_provisions,
                      predicted=retrieve_fn(i.query)) for i in evalset.items]


def run_evaluation(evalset: EvalSet, retrieve_fn: RetrieveFn, *,
                   model_snapshot_id: str, config: dict | None = None,
                   enforce_release_guard: bool = True,
                   repeat: int = 1) -> dict:
    """执行评测并返回双口径报告。

    `enforce_release_guard=True` 时，任何元数据/抽检/污染问题都会直接抛错，
    不会「先给个数字再说」。
    """
    config = config or {}
    checks: dict[str, object] = {}
    if enforce_release_guard:
        check_release_ready(evalset)
        checks["self_consistency"] = check_self_consistency(evalset)
        checks["human_accuracy"] = check_sampling(evalset)
        check_contamination(model_snapshot_id, evalset)

    rows = _collect(evalset, retrieve_fn)
    all_metrics = summarize(rows)
    human_rows = [r for r, item in zip(rows, evalset.items) if item.human_reviewed]
    human_metrics = summarize(human_rows)

    report: dict = {
        "set_id": evalset.set_id,
        "version": evalset.version,
        "content_hash": evalset.content_hash,
        "model_snapshot_id": model_snapshot_id,
        "config_hash": config_hash(config),
        "config": config,
        "ai_caliber": all_metrics,
        "human_caliber": human_metrics,
        "checks": checks,
    }
    if enforce_release_guard:
        report["caliber_diff"] = check_dual_caliber(all_metrics, human_metrics)

    if repeat > 1:
        again = summarize(_collect(evalset, retrieve_fn))
        report["stability_diff"] = check_stability(all_metrics, again)
        report["repeat_metrics"] = again

    # 记录接触面：dev 用于调参，golden 用于判据，二者不能共用同一快照
    record_contact(model_snapshot_id, evalset.set_id)
    return report
