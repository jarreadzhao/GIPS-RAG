"""评测集加载与发布判据守卫（AC-24 / 24A / 24C）。

两条物理隔离：
    - golden 只允许放在 `eval/golden/`，且必须带 `meta.json` 与 `frozen_at`；
    - dev 只允许放在 `eval/dev/`。

为什么不用一个文件加字段区分：只要两者能出现在同一目录，就一定会有人
拿 dev 调完提示词直接跑 golden，历史数字随即失去可比性。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

GOLDEN_DIR = Path("eval/golden")
DEV_DIR = Path("eval/dev")
EVAL_DIR = Path("eval")

# 抽检下限（AC-24B）
MIN_SAMPLE_RATIO = 0.20
MIN_SAMPLE_COUNT = 40
LOW_CONFIDENCE = 0.70
MIN_SELF_CONSISTENCY = 0.90
MIN_HUMAN_ACCURACY = 0.95
MAX_CALIBER_DIFF = 0.03


class EvalGuardError(ValueError):
    """评测集不满足发布判据；消息直接说明缺什么。"""


@dataclass
class EvalItem:
    """评测集一条。"""

    item_id: str
    query: str
    gold_provisions: list[str]
    labeler_model: str = ""
    labeler_prompt_hash: str = ""
    confidence: float = 1.0
    self_consistency: float = 1.0
    is_table_provision: bool = False
    human_reviewed: bool = False
    human_ok: bool | None = None
    source: str = "work_order"

    @property
    def needs_human(self) -> bool:
        """低置信或表格类条款必须进入抽检。"""
        return self.confidence < LOW_CONFIDENCE or self.is_table_provision


@dataclass
class EvalSet:
    """一个已加载的评测集。"""

    set_id: str
    version: str
    path: Path
    items: list[EvalItem] = field(default_factory=list)
    frozen_at: str | None = None
    model_snapshot_ids: list[str] = field(default_factory=list)

    @property
    def content_hash(self) -> str:
        """按条目内容计算哈希，任何改动都会变。"""
        raw = "\n".join(
            f"{i.item_id}|{i.query}|{','.join(i.gold_provisions)}" for i in self.items)
        return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def is_golden(self) -> bool:
        return self.path.parent.name == "golden"

    def load_meta(self) -> dict:
        meta_path = self.path.parent / "meta.json"
        return json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}


def _parse_item(raw: dict, line_no: int) -> EvalItem:
    try:
        item_id = str(raw["item_id"])
        query = str(raw["query"])
        gold = [str(p) for p in raw["gold_provisions"]]
    except (KeyError, TypeError) as exc:
        raise EvalGuardError(f"第 {line_no} 行缺少必填字段 {exc}") from exc
    if not gold:
        raise EvalGuardError(f"第 {line_no} 行 gold_provisions 为空")
    return EvalItem(
        item_id=item_id, query=query, gold_provisions=gold,
        labeler_model=str(raw.get("labeler_model") or ""),
        labeler_prompt_hash=str(raw.get("labeler_prompt_hash") or ""),
        confidence=float(raw.get("confidence", 1.0)),
        self_consistency=float(raw.get("self_consistency", 1.0)),
        is_table_provision=bool(raw.get("is_table_provision", False)),
        human_reviewed=bool(raw.get("human_reviewed", False)),
        human_ok=raw.get("human_ok"),
        source=str(raw.get("source", "work_order")),
    )


def load_set(path: Path) -> EvalSet:
    """加载 JSONL 评测集，并做目录与元数据校验。"""
    path = Path(path)
    if not path.exists():
        raise EvalGuardError(f"评测集不存在：{path}")
    if path.parent.name not in ("golden", "dev"):
        raise EvalGuardError(
            f"评测集必须放在名为 golden 或 dev 的目录下，当前为 {path.parent}；"
            "同一目录混放会导致 dev 调参后直接跑 golden")
    if path.parent.name == "dev" and path.parent.parent.name == "golden":
        raise EvalGuardError("dev 集不得放在 golden 目录内")

    items: list[EvalItem] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            items.append(_parse_item(json.loads(line), line_no))
    if not items:
        raise EvalGuardError(f"评测集为空：{path}")

    probe = EvalSet(set_id="", version="", path=path, items=items)
    meta = probe.load_meta()
    return EvalSet(
        set_id=str(meta.get("set_id") or path.stem),
        version=str(meta.get("version") or "1"),
        path=path,
        items=items,
        frozen_at=meta.get("frozen_at"),
        model_snapshot_ids=[str(x) for x in meta.get("model_snapshot_ids", [])],
    )


def check_release_ready(evalset: EvalSet) -> None:
    """AC-24：缺标注元数据或不是 golden 时拒绝出发布判据。"""
    if not evalset.is_golden:
        raise EvalGuardError("非 golden 集（dev）不得用于发布判据")
    if not evalset.frozen_at:
        raise EvalGuardError("golden 集未冻结：meta.json 缺少 frozen_at")
    missing_model = [i.item_id for i in evalset.items if not i.labeler_model]
    missing_hash = [i.item_id for i in evalset.items if not i.labeler_prompt_hash]
    if missing_model or missing_hash:
        raise EvalGuardError(
            "评测集缺少标注元数据："
            f"labeler_model 缺 {len(missing_model)} 条，"
            f"labeler_prompt_hash 缺 {len(missing_hash)} 条；"
            "补齐前无法判断历史数字与当前是否可比")


def check_self_consistency(evalset: EvalSet) -> float:
    """AC-24A：整批自一致率，低于 0.90 整批退回。"""
    mean = sum(i.self_consistency for i in evalset.items) / len(evalset.items)
    if mean < MIN_SELF_CONSISTENCY:
        raise EvalGuardError(
            f"自一致率 {mean:.3f} < {MIN_SELF_CONSISTENCY}，整批退回重标")
    return mean


def check_sampling(evalset: EvalSet) -> float:
    """AC-24B：抽检覆盖与准确率。

    覆盖要求：≥20% 且 ≥40 条；所有低置信项与所有表格类条款必须被抽到。
    """
    total = len(evalset.items)
    reviewed = [i for i in evalset.items if i.human_reviewed]
    required = max(MIN_SAMPLE_COUNT, math.ceil(total * MIN_SAMPLE_RATIO))
    if len(reviewed) < required:
        raise EvalGuardError(
            f"人工抽检 {len(reviewed)} 条 < 下限 {required} 条"
            f"（{MIN_SAMPLE_RATIO:.0%} 且不少于 {MIN_SAMPLE_COUNT} 条）")
    missed = [i.item_id for i in evalset.items if i.needs_human and not i.human_reviewed]
    if missed:
        raise EvalGuardError(
            f"低置信/表格类条款未抽检：{', '.join(missed[:10])}"
            f"{' 等' if len(missed) > 10 else ''}")
    if any(i.human_ok is None for i in reviewed):
        raise EvalGuardError("已抽检条目存在未填 human_ok 的记录")
    accuracy = sum(1 for i in reviewed if i.human_ok) / len(reviewed)
    if accuracy < MIN_HUMAN_ACCURACY:
        raise EvalGuardError(
            f"人工抽检准确率 {accuracy:.3f} < {MIN_HUMAN_ACCURACY}")
    return accuracy


def _contamination_path() -> Path:
    return EVAL_DIR / "contamination.json"


def record_contact(model_snapshot_id: str, set_id: str) -> None:
    """记录某模型快照接触过哪个评测集（AC-24C）。"""
    path = _contamination_path()
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data.setdefault(model_snapshot_id, [])
    if set_id not in data[model_snapshot_id]:
        data[model_snapshot_id].append(set_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def check_contamination(model_snapshot_id: str, evalset: EvalSet) -> None:
    """AC-24C：接触过 dev 的模型快照，不得直接对 golden 出判据。"""
    path = _contamination_path()
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    touched = data.get(model_snapshot_id, [])
    dev_sets = [s for s in touched if s != evalset.set_id]
    if dev_sets:
        raise EvalGuardError(
            f"模型快照 {model_snapshot_id} 已接触过 {', '.join(dev_sets)}；"
            "须换用未接触 golden 的版本，或重建 golden")
