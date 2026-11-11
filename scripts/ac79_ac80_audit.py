"""AC-79 / AC-80 全量审计（Q0 本期范围）。

AC-79：对本期「全部问答与走查记录」逐条做独立回验，引用可回链率必须 100%
       ——每条引用都能定位到原件页码与字符区间，`span_hash` 复算一致。
AC-80：答案不越界——无实数（只允许 `calc://` 占位）、无「已验证 / 合规通过」
       等效力性表述；检索为空时明确拒答且不含「通常 / 行业惯例 / 类似标准」。

两条硬约束：
    1. 只读数据库与原件，不调生成模型；本文件不 import app.reason 的生成链。
    2. 可举证范围由库里真实的记录决定。总体为空或不可见时记 N/A 并写明原因，
       不拿样本冒充全量，也不把 N/A 写成通过。

用法：
    python scripts/ac79_ac80_audit.py \
        --db var/gips.db --object-store var/objects \
        --out var/reports/ac79_ac80_audit.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.db import Database  # noqa: E402
from app.reason.verifier import Verifier  # noqa: E402

# 禁止出现的效力性表述（AC-80 原文口径）
FORBIDDEN = ("已验证", "合规通过", "通常", "行业惯例", "类似标准")

# 条款编号如 1.A.5。不能用 \b：中文紧邻数字时 CJK 也算 \w，边界不成立
PROVISION_RE = re.compile(r"\d+\.[A-Z]{1,2}\.\d+")
CALC_RE = re.compile(r"calc://[A-Za-z0-9_\-]+")           # 唯一允许的数值占位
HEX_RE = re.compile(r"\b[0-9a-f]{32,64}\b")               # ref_id / span_hash
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?\s*%?")
YEAR_RE = re.compile(r"^(19|20)\d{2}$")


def _hash(text: str) -> str:
    """span_hash 固定格式：sha256:<64 位小写十六进制>。"""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


# ---------- AC-79：引用回链 ----------
def _verify_anchor(db: Database, store: Path, anchor: dict) -> dict:
    """单条证据锚点的独立回验；不复用生产代码的比对结果，只复用其口径。"""
    ref_id = str(anchor.get("ref_id") or "(无 ref_id)")
    doc_id = str(anchor.get("doc_id") or "")
    version_no = int(anchor.get("version_no") or 1)
    page_no = int(anchor.get("page_no") or 0)
    char_start = int(anchor.get("char_start") or 0)
    char_end = int(anchor.get("char_end") or 0)
    span_text = str(anchor.get("span_text") or "")
    span_hash = str(anchor.get("span_hash") or "")

    row: dict = {"ref_id": ref_id, "doc_id": doc_id, "version_no": version_no,
                 "page_no": page_no, "char_start": char_start,
                 "char_end": char_end, "reason": "", "recomputed_hash": ""}

    doc = db.fetch_one(
        "SELECT doc_id, status FROM documents WHERE doc_id=? AND version_no=?",
        (doc_id, version_no))
    if doc is None:
        row["reason"] = "doc_not_registered"
        return row

    path = store / doc_id / f"v{version_no}" / "pages" / f"{page_no}.txt"
    if not path.exists():
        row["reason"] = "page_missing"
        return row
    source = path.read_text(encoding="utf-8")

    if char_end > len(source) or char_start > char_end:
        row["reason"] = "span_out_of_bounds"
        return row

    sliced = source[char_start:char_end]
    recomputed = _hash(sliced)
    row["recomputed_hash"] = recomputed
    if sliced != span_text:
        row["reason"] = "span_mismatch"
        return row
    if span_hash != recomputed:
        row["reason"] = "hash_mismatch"
        return row
    row["reason"] = ""
    return row


def audit_ac79(db: Database, store: Path, findings: list[dict],
               chains: list[dict], qa_tasks: list[dict]) -> dict:
    """逐条回验本期全部证据锚点（正式审查 + QA 引用）。"""
    by_finding: dict[str, list[dict]] = {}
    for step in chains:
        fid = step.get("finding_id")
        if fid and step.get("step_type") == "E":
            by_finding.setdefault(fid, []).extend(
                _json(step.get("anchors_json"), []))

    verifier = Verifier(db, store)
    rows: list[dict] = []
    for finding in findings:
        fid = finding["finding_id"]
        anchors = by_finding.get(fid, [])
        for anchor in anchors:
            result = _verify_anchor(db, store, anchor)
            result["finding_id"] = fid
            result["provision_id"] = finding["provision_id"]
            rows.append(result)

        # 与生产回验器交叉核对：两者结论必须一致，否则说明审计口径或回验器有偏
        steps = [{"step_type": s["step_type"],
                  "anchors": _json(s.get("anchors_json"), []),
                  "calc_ids": _json(s.get("payload_json"), {}).get("calc_ids") or []}
                 for s in chains if s.get("finding_id") == fid]
        verdict = verifier.verify(provision_id=finding["provision_id"],
                                  steps=steps, evidence_refs=anchors)
        for result in rows:
            if result["finding_id"] == fid:
                result["verifier_passed"] = verdict.passed
                result["verifier_failures"] = [f.reason for f in verdict.failures]

    # QA 引用：挂在 task 上的 E 步；没有 finding，逐条独立回验
    qa_rows: list[dict] = []
    qa_unverifiable: list[str] = []
    qa_chitchat: list[str] = []
    for task in qa_tasks:
        if str(_json(task.get("scope_json"), {}).get("intent") or "") == "chitchat":
            # 闲聊按设计不检索、不产生引用，既不计分母也不算「无法回验」
            qa_chitchat.append(task["task_id"])
            continue
        anchors: list[dict] = []
        answer_mode = ""
        for step in chains:
            if (step.get("task_id") == task["task_id"]
                    and step.get("step_type") == "E"
                    and not step.get("finding_id")):
                anchors.extend(_json(step.get("anchors_json"), []))
                answer_mode = str(_json(step.get("payload_json"), {}).get(
                    "answer_mode") or answer_mode)
        if not anchors and answer_mode != "refusal":
            # 旧实现只把条款编号拼成字符串，没有落引用；这类任务无法回验
            qa_unverifiable.append(task["task_id"])
        for anchor in anchors:
            result = _verify_anchor(db, store, anchor)
            result["task_id"] = task["task_id"]
            result["finding_id"] = None
            result["provision_id"] = str(anchor.get("provision_id") or "")
            failure = verifier.verify_ref(anchor)
            result["verifier_passed"] = failure is None
            result["verifier_failures"] = [] if failure is None else [failure.reason]
            qa_rows.append(result)
            # 中文对照同样逐条回验，计入同一分母
            for translation in anchor.get("translations") or []:
                tr = _verify_anchor(db, store, translation)
                tr["task_id"] = task["task_id"]
                tr["finding_id"] = None
                tr["provision_id"] = str(anchor.get("provision_id") or "")
                tr["translation_of"] = str(anchor.get("ref_id") or "")
                tr_failure = verifier.verify_ref(translation)
                tr["verifier_passed"] = tr_failure is None
                tr["verifier_failures"] = ([] if tr_failure is None
                                           else [tr_failure.reason])
                qa_rows.append(tr)

    all_rows = rows + qa_rows
    total = len(all_rows)
    passed = sum(1 for r in all_rows if not r["reason"])
    return {"total_anchors": total, "formal_anchors": len(rows),
            "qa_anchors": len(qa_rows),
            "translation_anchors": sum(1 for r in qa_rows
                                       if r.get("translation_of")),
            "passed": passed,
            "rate": (passed / total) if total else None,
            "qa_unverifiable": qa_unverifiable,
            "qa_chitchat": qa_chitchat,
            "rows": rows, "qa_rows": qa_rows}


# ---------- AC-80：输出越界 ----------
def _iter_model_texts(steps: list[dict]) -> list[tuple[str, str]]:
    """取出模型生成的文本字段。

    只取 L/A/C/J 四步的 payload；E 步的 span_text 是原件引文，不是模型输出，
    对它做数字扫描等于把被引用制度里的金额算到模型头上。
    """
    out: list[tuple[str, str]] = []
    for step in steps:
        step_type = str(step.get("step_type") or "")
        if step_type == "E":
            continue
        payload = _json(step.get("payload_json"), {})
        for key in ("reason", "verdict_reason", "status", "value", "verdict"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                out.append((f"{step_type}.{key}", value))
        for index, gap in enumerate(payload.get("gaps") or []):
            if isinstance(gap, dict):
                for key, value in gap.items():
                    if isinstance(value, str) and value.strip():
                        out.append((f"{step_type}.gaps[{index}].{key}", value))
    return out


def _scan_text(label: str, text: str) -> dict:
    """对一段模型输出做实数与禁用语扫描。"""
    stripped = CALC_RE.sub(" ", text)
    stripped = PROVISION_RE.sub(" ", stripped)
    stripped = HEX_RE.sub(" ", stripped)

    numbers: list[str] = []
    for match in NUMBER_RE.finditer(stripped):
        token = match.group(0).strip()
        # 年份不是金额/比率，单独列出以免把「GIPS 2020」判成越界
        if YEAR_RE.match(token):
            numbers.append(f"{token}(年份)")
        else:
            numbers.append(token)

    phrases = [p for p in FORBIDDEN if p in text]
    return {"where": label, "text": text, "numbers": numbers, "phrases": phrases}


def audit_ac80(db: Database, findings: list[dict], chains: list[dict],
               tasks: list[dict]) -> dict:
    """扫描本期全部 finding 与拒答记录的模型输出。"""
    by_finding: dict[str, list[dict]] = {}
    for step in chains:
        fid = step.get("finding_id")
        if fid:
            by_finding.setdefault(fid, []).append(step)

    scans: list[dict] = []
    for finding in findings:
        steps = by_finding.get(finding["finding_id"], [])
        for label, text in _iter_model_texts(steps):
            item = _scan_text(f"{finding['finding_id']}/{label}", text)
            item["finding_id"] = finding["finding_id"]
            scans.append(item)

    number_hits = [s for s in scans if s["numbers"]]
    phrase_hits = [s for s in scans if s["phrases"]]

    # 空检索拒答：formal 任务成功但无 finding，且链只到 A 步（2 步）
    abstained: list[dict] = []
    finding_tasks = {f["task_id"] for f in findings}
    for task in tasks:
        if task["task_type"] != "formal" or task["task_id"] in finding_tasks:
            continue
        steps = [s for s in chains if s.get("task_id") == task["task_id"]]
        step_types = sorted({str(s.get("step_type")) for s in steps})
        if task["status"] == "succeeded" and step_types == ["A", "L"]:
            abstained.append({"task_id": task["task_id"],
                              "scope": task["scope_json"],
                              "chain_steps": step_types,
                              # 拒答文案没有落库，无法核验其措辞
                              "answer_persisted": False})

    # QA 答案：抽句式答案 = 模板 + 引用原文。引文不是模型输出，扫描前先扣除；
    # 拒答文案整段都算模型侧措辞，必须原样扫描。
    qa_scans: list[dict] = []
    qa_abstained: list[dict] = []
    for task in [t for t in tasks if t["task_type"] == "qa"]:
        for step in chains:
            if (step.get("task_id") != task["task_id"]
                    or step.get("step_type") != "E"):
                continue
            payload = _json(step.get("payload_json"), {})
            anchors = _json(step.get("anchors_json"), [])
            mode = str(payload.get("answer_mode") or "")
            lead = str(payload.get("answer_lead") or "")
            residue = lead
            for anchor in anchors:
                residue = residue.replace(str(anchor.get("span_text") or ""), " ")
                for translation in anchor.get("translations") or []:
                    residue = residue.replace(
                        str(translation.get("span_text") or ""), " ")
            item = _scan_text(f"{task['task_id']}/qa_answer", residue)
            item["task_id"] = task["task_id"]
            item["answer_mode"] = mode
            qa_scans.append(item)
            if mode == "refusal":
                qa_abstained.append({"task_id": task["task_id"],
                                     "answer_mode": mode,
                                     "answer_persisted": True})

    return {"scanned_texts": len(scans), "number_hits": number_hits,
            "phrase_hits": phrase_hits, "abstained": abstained,
            "qa_scans": qa_scans,
            "qa_number_hits": [s for s in qa_scans if s["numbers"]],
            "qa_phrase_hits": [s for s in qa_scans if s["phrases"]],
            "qa_abstained": qa_abstained,
            "qa_unpersisted": [t["task_id"] for t in tasks
                               if t["task_type"] == "qa"
                               and str(_json(t.get("scope_json"), {}).get(
                                   "intent") or "") != "chitchat"
                               and not any(s.get("task_id") == t["task_id"]
                                           and s.get("step_type") == "E"
                                           for s in chains)],
            "qa_tasks": [t for t in tasks if t["task_type"] == "qa"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="AC-79 / AC-80 全量审计")
    parser.add_argument("--db", default="var/gips.db")
    parser.add_argument("--object-store", default="var/objects")
    parser.add_argument("--out", default="var/reports/ac79_ac80_audit.json")
    args = parser.parse_args()

    db = Database(Path(args.db))
    all_findings = [dict(r) for r in db.fetch_all(
        "SELECT * FROM findings ORDER BY created_at")]
    # 已作废的 finding 不进入审计总体：它的原件可能已被清除，纳入统计会把
    # 「记录本身已废」误报成「引用回链失败」
    voided = [f for f in all_findings if f.get("signoff_status") == "superseded"]
    findings = [f for f in all_findings if f.get("signoff_status") != "superseded"]
    chains = [dict(r) for r in db.fetch_all(
        "SELECT * FROM cot_chains ORDER BY created_at")]
    tasks = [dict(r) for r in db.fetch_all(
        "SELECT * FROM review_tasks ORDER BY queued_at")]
    qa_tasks = [t for t in tasks if t["task_type"] == "qa"]

    report = {
        "findings": len(findings),
        "voided_findings": [{"finding_id": f["finding_id"],
                             "provision_id": f["provision_id"],
                             "superseded_by": f.get("superseded_by")}
                            for f in voided],
        "review_tasks": len(tasks),
        "ac79": audit_ac79(db, Path(args.object_store), findings, chains,
                           qa_tasks),
        "ac80": audit_ac80(db, findings, chains, tasks),
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    ac79 = report["ac79"]
    rate = "N/A（本期无可回验锚点）" if ac79["rate"] is None else f"{ac79['rate']:.2%}"
    print(f"已作废 finding {len(report['voided_findings'])} 条，不进入总体")
    print(f"AC-79 证据锚点 {ac79['passed']}/{ac79['total_anchors']} 可回链，"
          f"可回链率 {rate}（正式审查 {ac79['formal_anchors']}，"
          f"QA 引用 {ac79['qa_anchors']}，其中中文对照 "
          f"{ac79['translation_anchors']}；"
          f"无引用不可回验的旧 QA 任务 {len(ac79['qa_unverifiable'])} 条；"
          f"闲聊不检索 {len(ac79['qa_chitchat'])} 条）")
    for row in ac79["rows"]:
        flag = "PASS" if not row["reason"] else f"FAIL({row['reason']})"
        print(f"  {flag:24s} {row['finding_id']} {row['doc_id']}"
              f" p{row['page_no']} [{row['char_start']}:{row['char_end']}]"
              f" verifier_pass={row.get('verifier_passed')}")
    for row in ac79["qa_rows"]:
        flag = "PASS" if not row["reason"] else f"FAIL({row['reason']})"
        print(f"  {flag:24s} QA {row['task_id']} {row['doc_id']}"
              f" p{row['page_no']} [{row['char_start']}:{row['char_end']}]"
              f" verifier_pass={row.get('verifier_passed')}")
    ac80 = report["ac80"]
    print(f"AC-80 扫描模型输出 {ac80['scanned_texts']} 段："
          f"实数命中 {len(ac80['number_hits'])}，"
          f"禁用语命中 {len(ac80['phrase_hits'])}，"
          f"空检索拒答 {len(ac80['abstained'])} 条，"
          f"QA 任务 {len(ac80['qa_tasks'])} 条")
    print(f"AC-80 QA 答案 {len(ac80['qa_scans'])} 段："
          f"实数命中 {len(ac80['qa_number_hits'])}，"
          f"禁用语命中 {len(ac80['qa_phrase_hits'])}，"
          f"明确拒答 {len(ac80['qa_abstained'])} 条，"
          f"答案未落库无法核验 {len(ac80['qa_unpersisted'])} 条")
    for hit in ac80["phrase_hits"] + ac80["qa_phrase_hits"]:
        print(f"  禁用语 {hit['phrases']} @ {hit['where']}: {hit['text']}")
    for hit in ac80["qa_number_hits"]:
        print(f"  QA 实数 {hit['numbers']} @ {hit['where']}: {hit['text']}")
    print(f"明细已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
