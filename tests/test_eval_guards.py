"""评测集守卫与双口径报告的验收（AC-13/14/24/24A-D）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.eval import dataset as ds
from app.eval.dataset import (EvalGuardError, EvalItem, check_release_ready,
                              check_sampling, check_self_consistency, load_set,
                              record_contact)
from app.eval.metrics import (MetricRow, check_dual_caliber, check_stability,
                              summarize)
from app.eval.runner import (config_hash, ensure_trusted_report, load_report,
                             run_evaluation)


@pytest.fixture()
def contamination(tmp_path, monkeypatch):
    """把污染台账重定向到临时目录，避免写进仓库。"""
    path = tmp_path / "eval" / "contamination.json"
    monkeypatch.setattr(ds, "_contamination_path", lambda: path)
    return path


def make_items(n: int = 200, *, reviewed: int = 40) -> list[EvalItem]:
    return [
        EvalItem(
            item_id=f"q{i:03d}", query=f"question {i}",
            gold_provisions=[f"1.A.{i % 20 + 1}"],
            labeler_model="qwen3-8b-q4_k_m",
            labeler_prompt_hash="sha256:abc",
            confidence=1.0, self_consistency=1.0,
            human_reviewed=i < reviewed, human_ok=True if i < reviewed else None,
        )
        for i in range(n)
    ]


def write_set(tmp_path: Path, folder: str, items: list[EvalItem],
              set_id: str = "golden-v1", frozen: bool = True) -> Path:
    directory = tmp_path / "eval" / folder
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{set_id}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item.__dict__, ensure_ascii=False) + "\n")
    (directory / "meta.json").write_text(json.dumps({
        "set_id": set_id, "version": "1",
        "frozen_at": "2026-11-09T00:00:00Z" if frozen else None,
    }), encoding="utf-8")
    return path


def test_golden_set_passes_all_guards(tmp_path, contamination):
    path = write_set(tmp_path, "golden", make_items())
    evalset = load_set(path)
    assert evalset.is_golden
    assert evalset.content_hash.startswith("sha256:")
    check_release_ready(evalset)
    assert check_self_consistency(evalset) == 1.0
    assert check_sampling(evalset) == 1.0


# ---------- AC-24：缺标注元数据必须拒绝 ----------
def test_missing_labeler_metadata_rejected(tmp_path, contamination):
    items = make_items()
    items[7].labeler_model = ""
    evalset = load_set(write_set(tmp_path, "golden", items))
    with pytest.raises(EvalGuardError, match="labeler_model"):
        check_release_ready(evalset)


def test_missing_prompt_hash_rejected(tmp_path, contamination):
    items = make_items()
    items[3].labeler_prompt_hash = ""
    evalset = load_set(write_set(tmp_path, "golden", items))
    with pytest.raises(EvalGuardError, match="labeler_prompt_hash"):
        check_release_ready(evalset)


def test_unfrozen_golden_rejected(tmp_path, contamination):
    evalset = load_set(write_set(tmp_path, "golden", make_items(), frozen=False))
    with pytest.raises(EvalGuardError, match="未冻结"):
        check_release_ready(evalset)


def test_dev_set_cannot_be_release_basis(tmp_path, contamination):
    evalset = load_set(write_set(tmp_path, "dev", make_items(), set_id="dev-v1"))
    with pytest.raises(EvalGuardError, match="非 golden"):
        check_release_ready(evalset)


def test_dataset_outside_golden_dev_rejected(tmp_path, contamination):
    directory = tmp_path / "eval"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "mixed.jsonl"
    path.write_text(json.dumps(make_items(3)[0].__dict__) + "\n", encoding="utf-8")
    with pytest.raises(EvalGuardError, match="golden 或 dev"):
        load_set(path)


# ---------- AC-24A：自一致率 ----------
def test_low_self_consistency_rejects_batch(tmp_path, contamination):
    items = make_items()
    for item in items[:70]:
        item.self_consistency = 0.667
    evalset = load_set(write_set(tmp_path, "golden", items))
    with pytest.raises(EvalGuardError, match="自一致率"):
        check_self_consistency(evalset)


# ---------- AC-24B：抽检覆盖与准确率 ----------
def test_sampling_below_minimum_rejected(tmp_path, contamination):
    evalset = load_set(write_set(tmp_path, "golden", make_items(reviewed=39)))
    with pytest.raises(EvalGuardError, match="下限"):
        check_sampling(evalset)


def test_low_confidence_item_must_be_reviewed(tmp_path, contamination):
    items = make_items()
    items[150].confidence = 0.33
    evalset = load_set(write_set(tmp_path, "golden", items))
    with pytest.raises(EvalGuardError, match="低置信"):
        check_sampling(evalset)


def test_table_provision_must_be_reviewed(tmp_path, contamination):
    items = make_items()
    items[151].is_table_provision = True
    evalset = load_set(write_set(tmp_path, "golden", items))
    with pytest.raises(EvalGuardError, match="低置信/表格类"):
        check_sampling(evalset)


def test_human_accuracy_below_threshold_rejected(tmp_path, contamination):
    items = make_items()
    for item in items[:3]:
        item.human_ok = False       # 37/40 = 0.925 < 0.95
    evalset = load_set(write_set(tmp_path, "golden", items))
    with pytest.raises(EvalGuardError, match="准确率"):
        check_sampling(evalset)


# ---------- AC-24C：dev 调参后的快照不得直接跑 golden ----------
def test_model_contacting_dev_is_blocked_on_golden(tmp_path, contamination):
    golden = load_set(write_set(tmp_path, "golden", make_items()))
    record_contact("snapshot-dev", "dev-v1")
    with pytest.raises(EvalGuardError, match="已接触过"):
        ds.check_contamination("snapshot-dev", golden)


# ---------- AC-24D / AC-13 / AC-14：双口径、Recall、稳定性 ----------
def test_dual_caliber_gap_rejects_report(tmp_path, contamination):
    evalset = load_set(write_set(tmp_path, "golden", make_items()))

    def retrieve(query: str) -> list[str]:
        idx = int(query.split()[-1])
        return ["WRONG"] if idx < 40 else [f"1.A.{idx % 20 + 1}"]

    with pytest.raises(EvalGuardError, match="相差"):
        run_evaluation(evalset, retrieve, model_snapshot_id="snap-golden",
                       enforce_release_guard=True)


def test_dual_caliber_report_and_metrics(tmp_path, contamination):
    evalset = load_set(write_set(tmp_path, "golden", make_items()))

    def retrieve(query: str) -> list[str]:
        idx = int(query.split()[-1])
        return [f"1.A.{idx % 20 + 1}"]

    report = run_evaluation(evalset, retrieve, model_snapshot_id="snap-golden",
                            config={"strategy": "provision"}, repeat=2)
    assert report["ai_caliber"]["recall@5"] == 1.0
    assert report["human_caliber"]["recall@5"] == 1.0
    assert report["caliber_diff"] == 0.0
    assert report["stability_diff"] == 0.0
    assert report["config_hash"].startswith("sha256:")


def test_metrics_na_for_empty_set():
    metrics = summarize([])
    assert metrics["recall@5"] is None and metrics["n"] == 0


def test_dual_caliber_requires_both_sides():
    with pytest.raises(EvalGuardError, match="缺少某一口径"):
        check_dual_caliber({"recall@5": 0.9}, {"recall@5": None})


def test_stability_gap_rejected():
    with pytest.raises(EvalGuardError, match="不稳定"):
        check_stability({"recall@5": 0.90}, {"recall@5": 0.95})


def test_rrf_metrics_shape():
    row = MetricRow(item_id="x", gold=["1.A.1"], predicted=["3.B.2", "1.A.1"])
    assert summarize([row])["recall@1"] == 0.0
    assert summarize([row])["recall@5"] == 1.0
    assert summarize([row])["mrr"] == 0.5


# ---------- AC-70：缺 config_hash 的评测结果不被采信 ----------
def test_report_missing_config_hash_rejected():
    """①缺 config_hash 的 report 直接拒绝。"""
    with pytest.raises(EvalGuardError, match="config_hash"):
        ensure_trusted_report({"set_id": "golden-v1", "ai_caliber": {}})
    # None 也视同缺失
    with pytest.raises(EvalGuardError, match="config_hash"):
        ensure_trusted_report({"set_id": "golden-v1", "config_hash": None})


def test_report_not_a_dict_rejected():
    """report 非 dict 同样拒绝（message 点明缺 config_hash）。"""
    with pytest.raises(EvalGuardError, match="config_hash"):
        ensure_trusted_report(["not", "a", "dict"])
    with pytest.raises(EvalGuardError, match="config_hash"):
        ensure_trusted_report("sha256:" + "a" * 64)


def test_report_malformed_config_hash_rejected():
    """②config_hash 非 sha256:[0-9a-f]{64} 格式被拒。"""
    bad_values = (
        "plain-hash",                          # 无前缀
        "sha256:" + "A" * 64,                  # 大写十六进制
        "sha256:" + "a" * 63,                  # 长度不足
        "sha256:" + "a" * 65,                  # 长度超出
        "md5:" + "a" * 32,                     # 错误算法前缀
    )
    for bad in bad_values:
        with pytest.raises(EvalGuardError, match="config_hash"):
            ensure_trusted_report({"set_id": "golden-v1", "config_hash": bad})


def test_load_report_trust_gate(tmp_path):
    """③写盘后经采信入口读取：合法报告返回内容，缺哈希报告抛 EvalGuardError。"""
    good = {"set_id": "golden-v1", "recall@5": 0.95,
            "config_hash": config_hash({"top_k": 20})}
    path = tmp_path / "report.json"
    path.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
    assert load_report(path) == good

    bad_path = tmp_path / "bad_report.json"
    bad_path.write_text(json.dumps({"set_id": "golden-v1"}), encoding="utf-8")
    with pytest.raises(EvalGuardError, match="config_hash"):
        load_report(bad_path)


def test_run_evaluation_report_passes_trust_guard(tmp_path, contamination):
    """④run_evaluation 产出的真实报告能通过 ensure_trusted_report。"""
    evalset = load_set(write_set(tmp_path, "golden", make_items()))

    def retrieve(query: str) -> list[str]:
        idx = int(query.split()[-1])
        return [f"1.A.{idx % 20 + 1}"]

    report = run_evaluation(evalset, retrieve, model_snapshot_id="snap-golden",
                            config={"strategy": "provision"})
    ensure_trusted_report(report)           # 不抛错即被采信

    path = tmp_path / "real_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    loaded = load_report(path)
    assert loaded["config_hash"] == report["config_hash"]
    assert loaded["ai_caliber"]["recall@5"] == 1.0
