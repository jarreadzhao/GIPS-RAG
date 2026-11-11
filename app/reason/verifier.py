"""独立回验器（T-2-05 / CT-3，AC-37、AC-38）。

三条硬规则：
    1. 只读原件与数据库，**不得调用生成模型**；本文件不 import llm / requests。
    2. 按 Unicode 码点切片后逐字节比较：不做空白归一、不做全半角转换、
       不做大小写折叠。
    3. 任一项失败即整链 fail，不提供「部分通过」；也不得顺手修正 span_text。

跨页证据：引用带 `page_end > page_no` 时必须给出 `page_spans`，
每页独立切片比对；回验通过时页码返回起止范围（AC-38）。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.db import Database


@dataclass
class VerificationFailure:
    """单条失败记录，字段与 CT-3 响应对齐。"""

    anchor_id: str
    reason: str          # anchor_missing / provision_not_found / span_mismatch
                         # / page_mismatch / calc_not_allowed
    expected: str = ""
    actual: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"anchor_id": self.anchor_id, "reason": self.reason,
                "expected": self.expected, "actual": self.actual}


@dataclass
class VerificationResult:
    """回验结果；passed=False 时 failures 非空。"""

    passed: bool
    failures: list[VerificationFailure] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"result": "pass" if self.passed else "fail",
                "failures": [f.as_dict() for f in self.failures]}


class Verifier:
    """锚点回验器。"""

    def __init__(self, db: Database, object_store_dir: Path) -> None:
        self._db = db
        self._store = Path(object_store_dir)

    # ---------- 对外入口 ----------
    def verify(self, *, provision_id: str, steps: list[dict[str, Any]],
               evidence_refs: list[dict[str, Any]],
               report_year: int | None = None) -> VerificationResult:
        """对一条 finding 草稿做全量回验。"""
        failures: list[VerificationFailure] = []

        if not self._provision_exists(provision_id):
            failures.append(VerificationFailure(
                anchor_id=f"provision:{provision_id}",
                reason="provision_not_found",
                expected="provisions 或 chunks 中存在该条款",
                actual="未找到",
            ))

        # 锚点结构缺失也必须回验失败，不能默认通过
        for step in steps:
            if not step.get("anchors"):
                failures.append(VerificationFailure(
                    anchor_id=f"step:{step.get('step_type', '?')}",
                    reason="anchor_missing", expected="至少一个锚点", actual="无",
                ))

        for index, ref in enumerate(evidence_refs):
            anchor_id = str(ref.get("ref_id") or f"evidence:{index}")
            if str(ref.get("page_end") or ref.get("page_no")) != str(ref.get("page_no")):
                failures.extend(self._verify_cross_page(anchor_id, ref))
            else:
                failure = self._verify_single_page(anchor_id, ref)
                if failure:
                    failures.append(failure)

        failures.extend(self._verify_calcs(steps))
        return VerificationResult(passed=not failures, failures=failures)

    def verify_ref(self, ref: dict[str, Any]) -> VerificationFailure | None:
        """单条引用回验。

        QA 引用与正式审查的证据锚点共用同一比对口径：只读原件、按码点切片、
        不修正传参。返回 None 表示通过。
        """
        anchor_id = str(ref.get("ref_id") or "ref")
        if str(ref.get("page_end") or ref.get("page_no")) != str(ref.get("page_no")):
            failures = self._verify_cross_page(anchor_id, ref)
            return failures[0] if failures else None
        return self._verify_single_page(anchor_id, ref)

    # ---------- 条款存在性 ----------
    def _provision_exists(self, provision_id: str) -> bool:
        row = self._db.fetch_one(
            "SELECT 1 FROM provisions WHERE provision_id=? LIMIT 1", (provision_id,))
        if row is not None:
            return True
        row = self._db.fetch_one(
            "SELECT 1 FROM chunks WHERE provision_id=? LIMIT 1", (provision_id,))
        return row is not None

    # ---------- 单页比对 ----------
    def _verify_single_page(self, anchor_id: str,
                            ref: dict[str, Any]) -> VerificationFailure | None:
        page_no = int(ref["page_no"])
        source = self._page_text(str(ref["doc_id"]), int(ref["version_no"]), page_no)
        if source is None:
            return VerificationFailure(anchor_id, "page_mismatch",
                                       expected=f"存在 {page_no} 页文本", actual="缺页")
        start, end = int(ref["char_start"]), int(ref["char_end"])
        if end > len(source):
            return VerificationFailure(anchor_id, "span_mismatch",
                                       expected=f"页长 {len(source)}", actual=f"终点 {end}")
        expected = source[start:end]
        actual = str(ref["span_text"])
        if expected != actual:
            return VerificationFailure(anchor_id, "span_mismatch",
                                       expected=expected, actual=actual)
        if ref.get("span_hash") and str(ref["span_hash"]) != _hash(actual):
            return VerificationFailure(anchor_id, "span_mismatch",
                                       expected=_hash(actual), actual=str(ref["span_hash"]))
        return None

    # ---------- 跨页比对 ----------
    def _verify_cross_page(self, anchor_id: str,
                           ref: dict[str, Any]) -> list[VerificationFailure]:
        spans = ref.get("page_spans")
        if not spans:
            return [VerificationFailure(anchor_id, "page_mismatch",
                                        expected="跨页引用需提供 page_spans", actual="缺失")]
        failures: list[VerificationFailure] = []
        for span in spans:
            single = {**ref, **span, "page_end": span.get("page_no")}
            failure = self._verify_single_page(anchor_id, single)
            if failure:
                failures.append(failure)
        return failures

    # ---------- 数值引用 ----------
    def _verify_calcs(self, steps: list[dict[str, Any]]) -> list[VerificationFailure]:
        failures: list[VerificationFailure] = []
        for step in steps:
            for calc_id in step.get("calc_ids") or []:
                row = self._db.fetch_one(
                    "SELECT engine_version FROM calc_results WHERE calc_id=?",
                    (str(calc_id),))
                if row is None:
                    failures.append(VerificationFailure(
                        anchor_id=f"calc:{calc_id}", reason="calc_not_allowed",
                        expected="calc_results 中存在该 calc_id", actual="未找到"))
        return failures

    # ---------- 页文本 ----------
    def _page_text(self, doc_id: str, version_no: int, page_no: int) -> str | None:
        path = self._store / doc_id / f"v{version_no}" / "pages" / f"{page_no}.txt"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")


def _hash(text: str) -> str:
    """span_hash 固定格式：sha256:<64 位小写十六进制>。"""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
