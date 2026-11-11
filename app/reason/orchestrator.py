"""单条款 L-A-E-C-J 编排器（T-2-02 / T-2-06 / T-2-07 / T-2-08）。

为什么强制三轮：
    4k 上下文装不下「条款 + profile + 证据 + 全链」，必须分段；分段后每轮之间
    只传结构化中间态（编号、页码、码点区间、比对状态），不把原文回灌。

执行顺序（不可交换）：
    1. 入模前注入检测
    2. 任务校验（profile / supply / ACL 由 API 层负责）
    3. 轮 1：L 定位 + A 适用性
    4. 轮 2：E 抽取 + C 比对
    5. 独立回验（不调模型；失败可重抽一次）
    6. 轮 3：J 判定 + 理由
    7. 数值扫描 → 效力性表述过滤 → 签署资格派生 → 落库

失败语义：
    - 结构错 -> ANCHOR_INVALID（重生成一次仍错）
    - 回验错 -> SPAN_MISMATCH（重抽一次仍错）
    - 数字错 -> CALC_NOT_ALLOWED
    以上失败都必须保留已生成的链步骤并标 invalidated（AC-40）。
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import Settings
from app.core.db import Database
from app.core.llm import LLMRequest, estimate_tokens
from app.core.logging import get_logger
from app.errors import AppError
from app.reason.anchor import validate_evidence_refs, validate_step_anchors
from app.reason.eligibility import (derive_eligibility, resolve_snapshot_for_request)
from app.reason.guard import (assert_no_injection, assert_numbers_allowed,
                              find_hints, find_speculative, filter_efficacy)
from app.reason.intent import classify_input, rewrite_query
from app.reason.prompts import cot_v1
from app.reason.verifier import Verifier

VALID_VERDICTS = ("SUFFICIENT", "PARTIAL", "GAP", "INSUFFICIENT_EVIDENCE")
VALID_APPLICABILITY = ("APPLIES", "NOT_APPLIES", "UNKNOWN")
PROVISION_RE = re.compile(r"\b\d+\.[A-Z]\.\d+\b")

# QA 回答只做抽取，不生成：由命中的条款原文组织，避免模型改写引入无依据结论
QA_ANSWER_LEAD = "以下为检索命中的条款原文，未做改写："
QA_ANSWER_LEAD_EN = "Answering with the retrieved provision text, unedited:"
QA_REFUSAL = "未在已入库的标准原文中检索到与该问题相关的条款，无法给出有原文依据的回答。"
QA_REFUSAL_EN = ("No provision relevant to this question was retrieved from the "
                 "ingested standard text, so no evidence-based answer can be given.")
# 命中条款存在、但中文译版未覆盖：不能让用户误以为「没检索到」
QA_ZH_UNAVAILABLE = ("检索命中的条款尚无中文译文，当前为中文模式，无法给出中文原文；"
                     "请切换到英文版查看原文。")
QA_CHITCHAT = "您好，请提出具体的合规审查问题或条款编号。"
QA_CHITCHAT_EN = "Please ask a specific compliance question or cite a provision number."


@dataclass
class ReviewRequest:
    """一次审查请求；formal 与 qa 共用入口。"""

    task_type: str                          # qa / formal
    scope: list[str] = field(default_factory=list)
    supply: str = "self_hosted"
    profile_version: int | None = None
    text: str = ""
    media: list[str] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    report_year: int | None = None
    # 答案语种：zh 只回中文字段与中文片段，en 只回英文；默认 zh
    answer_lang: str = "zh"
    claimed_inference_scope: str | None = None
    acl_scope: list[str] | None = None
    actor: str = "unknown"
    idem_key: str | None = None


@dataclass
class ReviewOutcome:
    """编排结果；steps 用于 API-8 与走查报告。"""

    task_id: str
    status: str
    rounds: int
    findings: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    business_code: str = ""
    intent: str = ""
    answer: str = ""
    citations: list[dict] = field(default_factory=list)
    rewritten_query: str = ""
    media_route: str = ""
    signoff_eligibility: str = ""
    warnings: list[str] = field(default_factory=list)
    replayed: bool = False


class ChainGenerator(Protocol):
    """分段生成协议；真实实现走 LLM，测试用脚本化替身。"""

    def round1(self, payload: dict) -> dict:
        ...

    def round2(self, payload: dict) -> dict:
        ...

    def round3(self, payload: dict) -> dict:
        ...


class LLMChainGenerator:
    """把三轮提示词交给生成后端，并解析 JSON。"""

    def __init__(self, llm: Any) -> None:
        self._llm = llm

    def round1(self, payload: dict) -> dict:
        return self._call(cot_v1.round1_prompt(payload))

    def round2(self, payload: dict) -> dict:
        return self._call(cot_v1.round2_prompt(payload))

    def round3(self, payload: dict) -> dict:
        return self._call(cot_v1.round3_prompt(payload))

    def _call(self, prompt: str) -> dict:
        result = self._llm.generate(LLMRequest(prompt=prompt, max_tokens=700,
                                               temperature=0.0))
        return _parse_json(result.text)


class ReviewOrchestrator:
    """单条款审查编排器。"""

    def __init__(self, db: Database, settings: Settings,
                 generator: ChainGenerator | None = None,
                 retrieval: Any | None = None) -> None:
        self._db = db
        self._settings = settings
        self._generator = generator
        self._retrieval = retrieval
        self._verifier = Verifier(db, settings.object_store_dir)
        self._log = get_logger("verify")

    # ---------- 入口 ----------
    async def run(self, req: ReviewRequest) -> ReviewOutcome:
        assert_no_injection(req.text)

        existing = self._replay(req)
        if existing is not None:
            return existing

        if req.task_type == "qa":
            return await self._run_qa(req)
        return self._run_formal(req)

    # ---------- QA 路径 ----------
    async def _run_qa(self, req: ReviewRequest) -> ReviewOutcome:
        intent = classify_input(req.text, req.media)
        rewrite = rewrite_query(req.text, req.history)
        snapshot, warning = resolve_snapshot_for_request(
            self._db, self._settings, req.claimed_inference_scope, req.actor)
        eligibility = derive_eligibility(self._db, snapshot)

        task_id = self._create_task(req, intent=intent.intent, status="running",
                                    snapshot=snapshot, eligibility=eligibility,
                                    scope_json={"question": req.text,
                                                "rewritten": rewrite.text,
                                                "intent": intent.intent})
        hits: list[dict] = []
        citations: list[dict] = []
        dropped_zh: list[str] = []
        answer = ""
        answer_mode = "chitchat"
        lang = req.answer_lang
        if intent.intent == "chitchat":
            # AC-31：不检索、不调生成模型
            answer = QA_CHITCHAT if lang == "zh" else QA_CHITCHAT_EN
        elif self._retrieval is not None:
            result = await self._retrieval.search(_retrieval_query(
                rewrite.text, req.acl_scope))
            hits = [h.as_dict() for h in result.reranked]
            citations, dropped_zh = self._collect_citations(result.reranked, lang)
            if citations:
                answer, answer_mode = _extractive_answer(citations, lang), "extractive"
            elif lang == "zh" and dropped_zh:
                # 命中存在但中文译版未覆盖，不能报「未检索到」
                answer, answer_mode = QA_ZH_UNAVAILABLE, "refusal"
            else:
                # AC-80：检索为空必须明确拒答，不得用先验知识补
                answer, answer_mode = _refusal(lang), "refusal"
            self._persist_qa_citations(task_id, citations, answer_mode, lang)

        self._finish_task(task_id, "succeeded")
        self._log.info("qa_finished", task_id=task_id, intent=intent.intent,
                       hits=len(hits), citations=len(citations),
                       media_route=intent.media_route)
        warnings = [warning] if warning else []
        warnings += [f"zh_unavailable:{p}" for p in dropped_zh]
        return ReviewOutcome(
            task_id=task_id, status="succeeded", rounds=0, intent=intent.intent,
            answer=answer, citations=citations, rewritten_query=rewrite.text,
            media_route=intent.media_route, signoff_eligibility=eligibility,
            warnings=warnings, steps=hits,
        )

    def _collect_citations(self, hits: list[Any],
                           lang: str = "en") -> tuple[list[dict], list[str]]:
        """把重排后的条款命中转成可回链引用。

        同一条款只取排在最前、且通过独立回验的块：重排已保证一个条款占一个
        名额，引用同样一对一，避免同一段原文在答案里重复。

        `lang=zh` 时按 `pair_id` 反查中文块并只返回中文；查不到或回验不通过的
        条款记入第二个返回值，由调用方决定是否降级，不能当作「没检索到」。
        """
        citations: list[dict] = []
        dropped: list[str] = []
        seen: set[str] = set()
        for hit in hits:
            provision_id = str(getattr(hit, "provision_id", "") or "")
            # 融合阶段会给无编号块（术语表、附录等）造 `chunk:<id>` 伪条款键。
            # 这类块能回验，但不满足 R-34 的「条款锚点」要求，不能当作条款引用；
            # 若一次检索全是这类块，citations 为空，由调用方按空结果拒答。
            if not provision_id or provision_id.startswith("chunk:"):
                continue
            rank = int(getattr(hit, "rank", 0) or 0)
            for chunk in getattr(hit, "chunks", []) or []:
                chunk_id = str(getattr(chunk, "chunk_id", "") or "")
                if not chunk_id or chunk_id in seen:
                    continue
                citation = self._citation_for_chunk(chunk_id, provision_id,
                                                    rank, lang)
                if citation is None:
                    dropped.append(provision_id)
                    continue
                seen.add(chunk_id)
                citations.append(citation)
                break
        return citations, dropped

    def _citation_for_chunk(self, chunk_id: str, provision_id: str,
                            rank: int, lang: str = "en") -> dict | None:
        """按 chunk_id 回库取定位字段，并用原件切片复算 span_hash。

        `lang=zh` 时按命中块的 `pair_id` 反查中文块，只返回中文片段；
        中文缺失或回验不通过时返回 None，由调用方区分「没检索到」与「没译文」。
        """
        row = self._db.fetch_one(
            "SELECT c.chunk_id, c.doc_id, c.version_no, c.provision_id, c.text, "
            "c.page_start, c.char_start, c.char_end, c.lang, c.pair_id "
            "FROM chunks c JOIN documents d "
            "ON d.doc_id = c.doc_id AND d.version_no = c.version_no "
            "WHERE c.chunk_id = ? AND c.status = 'active' "
            "AND d.status = 'published'", (chunk_id,))
        if row is None:
            self._log.warning("qa_citation_chunk_missing", chunk_id=chunk_id)
            return None
        if lang == "zh":
            pair_id = str(row["pair_id"] or "")
            row = self._zh_chunk_for(pair_id) if pair_id else None
            if row is None:
                self._log.warning("qa_zh_unavailable", chunk_id=chunk_id,
                                  pair_id=pair_id or "")
                return None
        text = str(row["text"])
        citation = {
            "type": "citation",
            "ref_id": str(row["chunk_id"]),
            "provision_id": str(row["provision_id"] or provision_id),
            "doc_id": str(row["doc_id"]),
            "version_no": int(row["version_no"]),
            "page_no": int(row["page_start"]),
            "char_start": int(row["char_start"]),
            "char_end": int(row["char_end"]),
            "lang": str(row["lang"] or ""),
            "rank": rank,
            "span_text": text,
            "span_hash": _span_hash(text),
            "translations": [],
        }
        failure = self._verifier.verify_ref(citation)
        if failure is not None:
            # 索引里有、原件对不上：说明索引与原件不一致，不能当作可引用答案
            self._log.warning("qa_citation_unverified", chunk_id=chunk_id,
                              reason=failure.reason)
            return None
        return citation

    def _zh_chunk_for(self, pair_id: str) -> dict | None:
        """取该 pair 下排在最前的中文显示层块；无译文时返回 None。"""
        return self._db.fetch_one(
            "SELECT c.chunk_id, c.doc_id, c.version_no, c.provision_id, c.text, "
            "c.page_start, c.char_start, c.char_end, c.lang FROM chunks c "
            "JOIN documents d ON d.doc_id = c.doc_id "
            "AND d.version_no = c.version_no "
            "WHERE c.pair_id = ? AND c.lang = 'zh' AND c.status = 'active' "
            "AND d.status = 'published' ORDER BY c.page_start, c.char_start "
            "LIMIT 1", (pair_id,))

    def _persist_qa_citations(self, task_id: str, citations: list[dict],
                              answer_mode: str, answer_lang: str) -> None:
        """QA 引用按 E 步落库，使 AC-79 与正式审查共用同一回验口径。

        payload 只放定位字段与答案模板，条款原文只出现在 anchors 里，
        不写进结构化中间态。
        """
        payload = {
            "answer_mode": answer_mode,
            "answer_lang": answer_lang,
            "answer_lead": (_extractive_lead(answer_lang)
                            if answer_mode == "extractive"
                            else _refusal(answer_lang)),
            "evidence_refs": [{k: c[k] for k in (
                "ref_id", "provision_id", "doc_id", "version_no", "page_no",
                "char_start", "char_end", "lang")} for c in citations],
            "translation_refs": [
                {**{k: t[k] for k in (
                    "ref_id", "doc_id", "version_no", "page_no",
                    "char_start", "char_end", "lang")},
                 "parent_ref_id": c["ref_id"]}
                for c in citations for t in c.get("translations") or []],
        }
        steps = [{"step_no": 1, "step_type": "E", "payload": payload,
                  "anchors": [{k: c[k] for k in (
                      "type", "ref_id", "provision_id", "doc_id", "version_no",
                      "page_no", "char_start", "char_end", "lang", "rank",
                      "span_text", "span_hash", "translations")}
                      for c in citations]}]
        self._persist_chain(task_id, None, steps, "pass")

    def _replay_qa(self, task_id: str, status: str) -> ReviewOutcome:
        """幂等回放：从已落库的引用重建答案，不重新检索。"""
        rows = self._db.fetch_all(
            "SELECT step_no, step_type, payload_json, anchors_json "
            "FROM cot_chains WHERE task_id=? ORDER BY step_no", (task_id,))
        citations: list[dict] = []
        steps: list[dict] = []
        answer_mode = ""
        answer_lang: str | None = None
        for row in rows:
            payload = _loads(row["payload_json"], {})
            anchors = _loads(row["anchors_json"], [])
            answer_mode = str(payload.get("answer_mode") or answer_mode)
            # 旧记录没有 answer_lang：保持 None，按当时的双语口径回放
            if "answer_lang" in payload:
                answer_lang = str(payload.get("answer_lang") or "zh")
            citations.extend(a for a in anchors if isinstance(a, dict))
            steps.append({"step_no": row["step_no"], "step_type": row["step_type"],
                          "payload": payload, "anchors": anchors})
        if answer_mode == "extractive":
            answer = _extractive_answer(citations, answer_lang)
        elif answer_mode == "refusal":
            answer = _refusal(answer_lang or "zh")
        else:
            answer = ""
        return ReviewOutcome(task_id=task_id, status=status, rounds=0,
                             answer=answer, citations=citations, steps=steps,
                             replayed=True)

    # ---------- formal 路径 ----------
    def _run_formal(self, req: ReviewRequest) -> ReviewOutcome:
        self._validate_formal(req)
        profile = self._load_profile(req.profile_version)
        snapshot, warning = resolve_snapshot_for_request(
            self._db, self._settings, req.claimed_inference_scope, req.actor)
        eligibility = derive_eligibility(self._db, snapshot)

        task_id = self._create_task(req, intent="formal", status="running",
                                    snapshot=snapshot, eligibility=eligibility,
                                    scope_json={"scope": req.scope})
        findings: list[dict] = []
        all_steps: list[dict] = []
        rounds = 0
        try:
            for provision_id in req.scope:
                outcome = self._review_one(task_id, provision_id, req, profile,
                                           snapshot, eligibility)
                rounds = max(rounds, int(outcome.get("rounds") or 0))
                if outcome["finding"]:
                    findings.append(outcome["finding"])
                all_steps.extend(outcome["steps"])
                if outcome.get("business_code"):
                    self._finish_task(task_id, "succeeded",
                                      business_code=outcome["business_code"])
                    return ReviewOutcome(
                        task_id=task_id, status="succeeded", rounds=rounds,
                        business_code=outcome["business_code"],
                        signoff_eligibility=eligibility,
                        warnings=[warning] if warning else [],
                    )
        except AppError as exc:
            # 失败也要让任务进入终态；链已在内部以 invalidated 落库
            self._finish_task(task_id, "failed")
            exc.details.setdefault("task_id", task_id)
            raise

        self._finish_task(task_id, "succeeded")
        return ReviewOutcome(
            task_id=task_id, status="succeeded", rounds=rounds, findings=findings,
            steps=all_steps, signoff_eligibility=eligibility,
            warnings=[warning] if warning else [],
        )

    def _review_one(self, task_id: str, provision_id: str, req: ReviewRequest,
                    profile: dict, snapshot: dict,
                    eligibility: str) -> dict:
        provision = self._load_provision(provision_id)
        steps: list[dict] = []

        # 轮 1：L + A（首轮允许带条款原文；轮间不回灌）
        payload1 = {
            "provision_id": provision_id,
            "report_year": req.report_year,
            "provision": provision,
            "profile": profile,
        }
        self._assert_budget(payload1, snapshot)
        locate, applicability = self._round1_with_retry(payload1, provision_id)

        app_value = str(applicability.get("value") or "")
        # 生效期硬判定：覆盖不了报告期就是 NOT_APPLIES，不交给模型猜
        if app_value == "APPLIES" and not _effective_covers(provision, req.report_year):
            app_value = "NOT_APPLIES"
            applicability = {**applicability, "value": app_value,
                             "reason": "条款生效期不覆盖报告期"}

        steps.append({"step_no": 1, "step_type": "L", "payload": locate,
                      "anchors": locate.get("anchors") or []})
        steps.append({"step_no": 2, "step_type": "A", "payload": applicability,
                      "anchors": applicability.get("anchors") or []})

        if app_value == "NOT_APPLIES":
            # AC-41/42：链在 A 步终止，且不得夹带提示性缺口
            text = str(applicability.get("reason") or "")
            if find_hints(text):
                raise AppError("VALIDATION_FAILED", "NOT_APPLIES 输出含提示性缺口")
            self._persist_chain(task_id, None, steps, "pass")
            self._log.info("review_not_applies", task_id=task_id,
                           provision_id=provision_id)
            return {"finding": None, "steps": _public_steps(steps), "rounds": 2}

        if app_value == "UNKNOWN":
            missing = list(applicability.get("missing_profile_fields") or [])
            gaps = [{"required_action": "补齐机构画像字段",
                     "current_practice": "profile 未覆盖该字段",
                     "evidence_needed": ", ".join(missing) or "profile",
                     "owner": "合规部"}]
            finding = self._persist_finding(task_id, provision_id, steps, gaps,
                                            "INSUFFICIENT_EVIDENCE",
                                            "适用性无法判定，需补齐机构画像",
                                            [], profile, snapshot, eligibility)
            return {"finding": finding, "steps": _public_steps(steps), "rounds": 2}

        # 轮 2：E + C
        candidates = self._evidence_candidates(provision_id)
        if not candidates:
            # AC-45：检索为空，拒答，不用先验知识补
            self._persist_chain(task_id, None, steps, "pass")
            self._log.warning("retrieval_empty_refused", provision_id=provision_id)
            return {"finding": None, "steps": _public_steps(steps), "rounds": 2,
                    "business_code": "RETRIEVAL_EMPTY"}

        payload2 = {"provision_id": provision_id,
                    "applicability": app_value,
                    "evidence_candidates": [_strip_text(c) for c in candidates]}
        self._assert_budget(payload2, snapshot)
        refs, comparisons = self._round2_with_retry(payload2, candidates)
        steps.append({"step_no": 3, "step_type": "E",
                      "payload": {"evidence_refs": [_strip_text(r) for r in refs]},
                      "anchors": [{"type": "evidence", **r} for r in refs]})
        steps.append({"step_no": 4, "step_type": "C",
                      "payload": {"comparisons": comparisons},
                      "anchors": [{"type": "comparison",
                                   "comparison_id": _id("cmp")} for _ in comparisons]})

        # 独立回验：失败重抽一次，仍失败整链作废（AC-37/39/40）
        verdict = self._verifier.verify(provision_id=provision_id, steps=steps,
                                        evidence_refs=refs,
                                        report_year=req.report_year)
        if not verdict.passed:
            self._log.warning("anchor_verification_failed", task_id=task_id,
                              provision_id=provision_id,
                              reason=verdict.failures[0].reason)
            payload2_retry = {**payload2,
                              "verification_failures": [f.as_dict()
                                                        for f in verdict.failures]}
            self._assert_budget(payload2_retry, snapshot)
            refs, comparisons = self._round2_with_retry(payload2_retry, candidates)
            steps[2] = {"step_no": 3, "step_type": "E",
                        "payload": {"evidence_refs": [_strip_text(r) for r in refs]},
                        "anchors": [{"type": "evidence", **r} for r in refs]}
            steps[3] = {"step_no": 4, "step_type": "C",
                        "payload": {"comparisons": comparisons},
                        "anchors": [{"type": "comparison",
                                     "comparison_id": _id("cmp")}
                                    for _ in comparisons]}
            verdict = self._verifier.verify(
                provision_id=provision_id, steps=steps,
                evidence_refs=refs, report_year=req.report_year)
            if not verdict.passed:
                self._persist_chain(task_id, None, steps, "invalidated")
                raise AppError(
                    "SPAN_MISMATCH",
                    f"回验失败且重抽一次仍不通过：{verdict.failures[0].reason}",
                    details={"failures": [f.as_dict() for f in verdict.failures]},
                )

        # 轮 3：J
        payload3 = {"provision_id": provision_id,
                    "comparisons": comparisons,
                    "evidence_ids": [r["ref_id"] for r in refs]}
        self._assert_budget(payload3, snapshot)
        r3 = self._round3_with_retry(payload3)
        verdict_value = str(r3.get("verdict") or "")
        if verdict_value not in VALID_VERDICTS:
            raise AppError("VALIDATION_FAILED", "J 步判定不在四态之内")
        gaps = list(r3.get("gaps") or [])
        if verdict_value == "PARTIAL" and not _splittable(gaps):
            # AC-44：PARTIAL 拆不出缺失项 -> GAP
            verdict_value = "GAP"
            gaps = gaps or [{"required_action": "补充可分割的缺失项",
                             "current_practice": "未提供",
                             "evidence_needed": "缺失项清单", "owner": "合规部"}]
        if verdict_value == "INSUFFICIENT_EVIDENCE" and not _splittable(gaps):
            raise AppError("VALIDATION_FAILED",
                           "INSUFFICIENT_EVIDENCE 必须给出应补证据类型与责任方")

        steps.append({"step_no": 5, "step_type": "J",
                      "payload": {"verdict": verdict_value,
                                  "verdict_reason": r3.get("verdict_reason", ""),
                                  "gaps": gaps},
                      # 页码用条款规范值，不采信模型自报的 page_no（可能写错）
                      "anchors": [{"type": "provision",
                                   "provision_id": provision_id,
                                   "page_no": provision["page_no"]}]})

        # 数字扫描在落库前；效力性表述在落库前替换（AC-36/46）
        steps = _filter_steps(steps)
        assert_numbers_allowed(_chain_scan_payload(steps, r3))
        reason, _hits = filter_efficacy(str(r3.get("verdict_reason") or ""))

        finding = self._persist_finding(
            task_id, provision_id, steps, gaps, verdict_value, reason,
            [r["ref_id"] for r in refs], profile, snapshot, eligibility)
        return {"finding": finding, "steps": _public_steps(steps), "rounds": 3}

    # ---------- 分段重试：任何一段结构非法都重生成一次 ----------
    def _round1_with_retry(self, payload: dict, provision_id: str) -> tuple[dict, dict]:
        last: AppError | None = None
        for _ in (1, 2):
            r1 = self._generate_with_retry("round1", payload)
            try:
                locate = r1.get("locate") or {}
                applicability = r1.get("applicability") or {}
                if str(locate.get("provision_id") or "") != provision_id:
                    raise AppError("ANCHOR_INVALID", "L 步条款编号与请求不一致")
                if str(applicability.get("value") or "") not in VALID_APPLICABILITY:
                    raise AppError("ANCHOR_INVALID", "A 步适用性不是三态之一")
                validate_step_anchors("L", locate.get("anchors") or [])
                validate_step_anchors("A", applicability.get("anchors") or [])
                return locate, applicability
            except AppError as exc:
                last = exc
        assert last is not None
        raise last

    def _round2_with_retry(self, payload: dict,
                           candidates: list[dict]) -> tuple[list[dict], list[dict]]:
        last: AppError | None = None
        for _ in (1, 2):
            r2 = self._generate_with_retry("round2", payload)
            try:
                refs = self._resolve_refs(r2.get("evidence_refs") or [], candidates)
                comparisons = list(r2.get("comparisons") or [])
                if not comparisons:
                    raise AppError("ANCHOR_INVALID", "C 步没有任何比对结果")
                for ref in refs:
                    validate_step_anchors("E", [{"type": "evidence", **ref}])
                validate_step_anchors("C", [{"type": "comparison",
                                             "comparison_id": "placeholder"}])
                return refs, comparisons
            except AppError as exc:
                last = exc
        assert last is not None
        raise last

    def _round3_with_retry(self, payload: dict) -> dict:
        last: AppError | None = None
        for _ in (1, 2):
            r3 = self._generate_with_retry("round3", payload)
            try:
                validate_step_anchors("J", r3.get("anchors") or [])
                return r3
            except AppError as exc:
                last = exc
        assert last is not None
        raise last

    # ---------- 校验 ----------
    def _validate_formal(self, req: ReviewRequest) -> None:
        if req.supply == "managed":
            raise AppError("SUPPLY_NOT_ALLOWED", "formal 任务不允许 managed 供给")
        if req.profile_version is None:
            raise AppError("PROFILE_MISSING", "formal 任务必须指定 profile_version")
        if not req.scope:
            raise AppError("VALIDATION_FAILED", "formal 任务 scope 不能为空")
        if len(req.scope) > 50:
            raise AppError("VALIDATION_FAILED", "单任务 scope 不得超过 50 条")

    def _load_profile(self, version: int | None) -> dict:
        row = self._db.fetch_one(
            "SELECT * FROM firm_profiles WHERE profile_version=?", (version,))
        if row is None:
            raise AppError("PROFILE_MISSING", f"profile_version={version} 不存在")
        profile = dict(row)
        if isinstance(profile.get("asset_classes"), str):
            try:
                profile["asset_classes"] = json.loads(profile["asset_classes"])
            except json.JSONDecodeError:
                profile["asset_classes"] = []
        return profile

    def _load_provision(self, provision_id: str) -> dict:
        row = self._db.fetch_one(
            "SELECT * FROM provisions WHERE provision_id=? "
            "ORDER BY version_no DESC LIMIT 1", (provision_id,))
        if row is not None:
            return {k: row[k] for k in row.keys()}
        # M0 尚未回填 provisions 时，从 chunk 兜底；只允许 A_spec，
        # 否则可能取到机构自己的制度片段，让模型「用证据证明证据」（AC-45 反面）。
        chunk = self._db.fetch_one(
            "SELECT c.chunk_id, c.doc_id, c.version_no, c.text, c.page_start, "
            "c.page_end, c.char_start, c.char_end "
            "FROM chunks c JOIN documents d "
            "ON d.doc_id = c.doc_id AND d.version_no = c.version_no "
            "WHERE c.provision_id = ? AND c.status = 'active' "
            "AND d.corpus_type = 'A_spec' "
            "ORDER BY c.chunk_id LIMIT 1", (provision_id,))
        if chunk is None:
            raise AppError("ANCHOR_INVALID",
                           f"条款 {provision_id} 在索引与条款树中均不存在")
        return {"provision_id": provision_id, "version_no": chunk["version_no"],
                "text_original": chunk["text"], "page_no": chunk["page_start"],
                "effective_from": "", "effective_to": None}

    def _evidence_candidates(self, provision_id: str) -> list[dict]:
        """证据只能是机构自己的制度（B_internal），不能是 GIPS 标准原文。

        A_spec 是被审查的标准本身；若拿它当证据，模型看到的就是「条款 vs 它自己」，
        必然判 SUFFICIENT，属于虚假合规结论。因此这里 JOIN documents 显式限定
        corpus_type='B_internal'，机构侧没有对应制度时返回空集，由调用方走
        RETRIEVAL_EMPTY（AC-45）。
        """
        rows = self._db.fetch_all(
            "SELECT c.chunk_id, c.doc_id, c.version_no, c.text, c.page_start, "
            "c.char_start, c.char_end "
            "FROM chunks c JOIN documents d "
            "ON d.doc_id = c.doc_id AND d.version_no = c.version_no "
            "WHERE c.provision_id = ? AND c.status = 'active' "
            "AND d.corpus_type = 'B_internal' "
            "ORDER BY c.chunk_id LIMIT 10", (provision_id,))
        return [{"ref_id": r["chunk_id"], "doc_id": r["doc_id"],
                 "version_no": r["version_no"], "page_no": r["page_start"],
                 "char_start": r["char_start"], "char_end": r["char_end"],
                 "span_text": r["text"]} for r in rows]

    def _resolve_refs(self, refs: list[dict], candidates: list[dict]) -> list[dict]:
        """模型只给 ref_id 时，从候选补齐原文与偏移；补齐后仍缺字段即非法。"""
        by_id = {c["ref_id"]: c for c in candidates}
        resolved: list[dict] = []
        for ref in refs:
            ref_id = str(ref.get("ref_id") or ref.get("evidence_id") or "")
            base = by_id.get(ref_id)
            if base is None and not ref.get("span_text"):
                raise AppError("ANCHOR_INVALID", f"证据引用 {ref_id!r} 不在候选集中")
            merged = dict(base or {})
            merged.update({k: v for k, v in ref.items() if v not in (None, "")})
            merged.setdefault("ref_id", ref_id or _id("ev"))

            spans = merged.get("page_spans")
            if spans:
                # 跨页：span_text 只用校验，不参与轮间传输；
                # 每页必须自带本页哈希，回验器逐页独立比对（AC-38）。
                for span in spans:
                    span["span_hash"] = _span_hash(str(span.get("span_text", "")))
                merged.setdefault(
                    "span_text", "".join(str(s.get("span_text", "")) for s in spans))
                merged.setdefault("page_end",
                                  max(int(s["page_no"]) for s in spans))
            merged["span_hash"] = _span_hash(str(merged["span_text"]))
            resolved.append(merged)
        return validate_evidence_refs(resolved)

    def _generate_with_retry(self, round_name: str, payload: dict) -> dict:
        """结构生成失败重试一次；脚本化生成器同样走这条路径。"""
        generator = self._generator
        if generator is None:
            raise AppError("VALIDATION_FAILED", "未配置生成器，无法执行审查")
        for attempt in (1, 2):
            result = getattr(generator, round_name)(payload)
            if not isinstance(result, dict):
                if attempt == 1:
                    continue
                raise AppError("VALIDATION_FAILED", f"{round_name} 输出不是 JSON 对象")
            return result
        raise AppError("VALIDATION_FAILED", f"{round_name} 重试后仍无有效输出")

    def _assert_budget(self, payload: dict, snapshot: dict) -> None:
        """AC-47A：超预算直接拒绝，绝不静默截断。"""
        ctx_limit = int(snapshot.get("ctx_limit") or self._settings.llm_n_ctx)
        ratio = float(self._settings.llm_ctx_budget_ratio)
        tokens = estimate_tokens(json.dumps(payload, ensure_ascii=False))
        if tokens > ctx_limit * ratio:
            raise AppError("CONTEXT_BUDGET_EXCEEDED",
                           f"单轮输入约 {tokens} tokens，超过上下文预算 "
                           f"{int(ctx_limit * ratio)}，拒绝截断执行",
                           details={"tokens_in": tokens, "ctx_limit": ctx_limit})

    # ---------- 落库 ----------
    def _replay(self, req: ReviewRequest) -> ReviewOutcome | None:
        """同一 Idempotency-Key 直接回放；请求指纹不同则冲突。"""
        if not req.idem_key:
            return None
        row = self._db.fetch_one(
            "SELECT task_id FROM idempotency_keys WHERE idem_key=?", (req.idem_key,))
        if row is None:
            return None
        task = self._db.fetch_one(
            "SELECT * FROM review_tasks WHERE task_id=?", (row["task_id"],))
        if task is None:
            return None
        try:
            stored = json.loads(task["scope_json"])
        except (TypeError, json.JSONDecodeError):
            stored = {}
        if stored.get("fingerprint") != _fingerprint(req):
            raise AppError("IDEMPOTENCY_CONFLICT",
                           "同一幂等键对应了不同的请求体")
        if task["task_type"] == "qa":
            return self._replay_qa(task["task_id"], task["status"])
        findings = self._load_findings(task["task_id"])
        return ReviewOutcome(task_id=task["task_id"], status=task["status"],
                             rounds=3, findings=findings, replayed=True,
                             signoff_eligibility=(findings[0]["signoff_eligibility"]
                                                  if findings else ""))

    def _create_task(self, req: ReviewRequest, *, intent: str, status: str,
                     snapshot: dict, eligibility: str,
                     scope_json: dict) -> str:
        task_id = _id("tsk")
        now = _now()
        scope_json = {**scope_json, "fingerprint": _fingerprint(req)}
        scope_text = json.dumps(scope_json, ensure_ascii=False, sort_keys=True,
                                default=str)

        def _write(conn) -> None:
            if req.idem_key:
                existing = conn.execute(
                    "SELECT task_id FROM idempotency_keys WHERE idem_key=?",
                    (req.idem_key,)).fetchone()
                if existing is not None:
                    raise AppError("IDEMPOTENCY_CONFLICT", "幂等键已被其他任务占用")
            conn.execute(
                "INSERT INTO review_tasks (task_id, task_type, supply, "
                "inference_scope, scope_json, profile_version, status, "
                "queued_at, started_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (task_id, req.task_type, req.supply,
                 snapshot["inference_scope"], scope_text, req.profile_version,
                 status, now, now),
            )
            if req.idem_key:
                conn.execute(
                    "INSERT INTO idempotency_keys (idem_key, task_id, created_at) "
                    "VALUES (?,?,?)", (req.idem_key, task_id, now))

        self._db.run_write(_write)
        self._log.info("review_task_created", task_id=task_id,
                       task_type=req.task_type, intent=intent)
        return task_id

    def _finish_task(self, task_id: str, status: str,
                     business_code: str | None = None) -> None:
        """终态落库；business_code 仅在有值时写入，不覆盖已有码（DEF-Q0-03）。"""
        self._db.run_write(lambda conn: conn.execute(
            "UPDATE review_tasks SET status=?, finished_at=?, "
            "business_code=COALESCE(?, business_code) WHERE task_id=?",
            (status, _now(), business_code, task_id)))

    def _persist_chain(self, task_id: str, finding_id: str | None,
                       steps: list[dict], validator: str) -> None:
        """append-only 写链；prev_hash 形成哈希链。"""
        created = _now()
        prev = self._db.fetch_one(
            "SELECT self_hash FROM cot_chains WHERE task_id=? "
            "ORDER BY step_no DESC LIMIT 1", (task_id,))
        prev_hash = prev["self_hash"] if prev else None
        rows = []
        for step in steps:
            payload_json = json.dumps(step["payload"], ensure_ascii=False,
                                      sort_keys=True)
            anchors_json = json.dumps(step["anchors"], ensure_ascii=False,
                                      sort_keys=True)
            raw = f"{prev_hash or ''}|{step['step_no']}|{step['step_type']}|" \
                  f"{payload_json}|{anchors_json}|{validator}"
            self_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            rows.append((_id("chn"), task_id, finding_id, step["step_no"],
                         step["step_type"], payload_json, anchors_json,
                         validator, prev_hash, self_hash, created))
            prev_hash = self_hash

        self._db.run_write(lambda conn: conn.executemany(
            "INSERT INTO cot_chains (chain_id, task_id, finding_id, step_no, "
            "step_type, payload_json, anchors_json, validator_result, prev_hash, "
            "self_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows))

    def _persist_finding(self, task_id: str, provision_id: str,
                         steps: list[dict], gaps: list[dict], verdict: str,
                         reason: str, ref_ids: list[str], profile: dict,
                         snapshot: dict, eligibility: str) -> dict:
        finding_id = _id("fnd")
        now = _now()

        def _write(conn) -> None:
            conn.execute(
                "INSERT INTO findings (finding_id, task_id, provision_id, verdict, "
                "verdict_reason, evidence_refs, calc_ids, profile_version, "
                "model_snapshot_id, prompt_version, signoff_status, "
                "signoff_eligibility, row_version, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
                (finding_id, task_id, provision_id, verdict, reason[:2000],
                 json.dumps(ref_ids, ensure_ascii=False), "[]",
                 profile["profile_version"], snapshot["snapshot_id"],
                 cot_v1.PROMPT_VERSION, "draft", eligibility, now),
            )
            for gap in gaps:
                conn.execute(
                    "INSERT INTO gaps_register (gap_id, finding_id, required_action, "
                    "current_practice, evidence_needed, owner, status) "
                    "VALUES (?,?,?,?,?,?,'open')",
                    (_id("gap"), finding_id, str(gap.get("required_action", "")),
                     str(gap.get("current_practice", "")),
                     str(gap.get("evidence_needed", "")),
                     str(gap.get("owner", ""))),
                )

        self._db.run_write(_write)
        self._persist_chain(task_id, finding_id, steps, "pass")
        return {"finding_id": finding_id, "provision_id": provision_id,
                "verdict": verdict, "verdict_reason": reason,
                "signoff_eligibility": eligibility, "gaps": gaps}

    def _load_findings(self, task_id: str) -> list[dict]:
        rows = self._db.fetch_all(
            "SELECT * FROM findings WHERE task_id=? ORDER BY finding_id", (task_id,))
        return [dict(r) for r in rows]


# ---------- 纯函数工具 ----------
def _parse_json(text: str) -> dict:
    clean = (text or "").strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```[a-zA-Z]*\n?|```$", "", clean).strip()
    start, end = clean.find("{"), clean.rfind("}")
    if start < 0 or end <= start:
        raise AppError("VALIDATION_FAILED", "生成结果不是合法 JSON 对象")
    try:
        value = json.loads(clean[start:end + 1])
    except json.JSONDecodeError as exc:
        raise AppError("VALIDATION_FAILED", f"生成结果 JSON 解析失败：{exc}") from exc
    if not isinstance(value, dict):
        raise AppError("VALIDATION_FAILED", "生成结果不是 JSON 对象")
    return value


def _effective_covers(provision: dict, report_year: int | None) -> bool:
    if report_year is None:
        return True
    start = str(provision.get("effective_from") or "")
    end = str(provision.get("effective_to") or "")
    start_year = _year(start)
    end_year = _year(end)
    if start_year and report_year < start_year:
        return False
    if end_year and report_year > end_year:
        return False
    return True


def _year(value: str) -> int | None:
    match = re.match(r"(\d{4})", value or "")
    return int(match.group(1)) if match else None


def _splittable(gaps: list[dict]) -> bool:
    return bool(gaps) and all(g.get("required_action") and g.get("evidence_needed")
                              and g.get("owner") for g in gaps)


def _strip_text(ref: dict) -> dict:
    """轮间只传引用，不传证据原文。"""
    return {k: ref[k] for k in ("ref_id", "doc_id", "version_no", "page_no",
                                "char_start", "char_end") if k in ref}


def _filter_steps(steps: list[dict]) -> list[dict]:
    """效力性表述在落库前替换；原文不写入台账。"""
    filtered: list[dict] = []
    for step in steps:
        filtered.append({**step,
                         "payload": _filter_obj(step.get("payload")),
                         "anchors": step.get("anchors") or []})
    return filtered


def _filter_obj(value: Any) -> Any:
    if isinstance(value, str):
        return filter_efficacy(value)[0]
    if isinstance(value, list):
        return [_filter_obj(v) for v in value]
    if isinstance(value, dict):
        return {k: _filter_obj(v) for k, v in value.items()}
    return value


def _chain_scan_payload(steps: list[dict], r3: dict) -> dict:
    """数字扫描对象：链上全部结构化字段 + J 步原始输出。"""
    return {"steps": [s.get("payload") for s in steps],
            "verdict_reason": r3.get("verdict_reason", ""),
            "gaps": r3.get("gaps", [])}


def _public_steps(steps: list[dict]) -> list[dict]:
    return [{"step_no": s["step_no"], "step_type": s["step_type"],
             "payload": s.get("payload"), "anchors": s.get("anchors")}
            for s in steps]


def _extractive_lead(lang: str) -> str:
    return QA_ANSWER_LEAD if lang == "zh" else QA_ANSWER_LEAD_EN


def _refusal(lang: str) -> str:
    return QA_REFUSAL if lang == "zh" else QA_REFUSAL_EN


def _extractive_answer(citations: list[dict], lang: str | None = "zh") -> str:
    """抽句式回答：只拼接已回验的原文片段，不生成、不改写、不补结论。

    `lang` 决定模板语言与片段语种：
      - zh：中文模板 + 中文字段（由 orchestrator 按 pair_id 替换过原文块）
      - en：英文模板 + 英文原文
      - None：回放旧记录，保持当时的「英文原文 + 中文：对照」口径
    """
    if not citations:
        return _refusal(lang or "zh")
    if lang is None:
        # 旧记录（payload 无 answer_lang）按双语口径回放，避免同一 task 回放结果与首次不一致
        blocks: list[str] = []
        shown: set[str] = set()
        for citation in citations:
            block = f"{citation['provision_id']}：{citation['span_text']}"
            fresh = [t for t in citation.get("translations") or []
                     if t["span_text"] not in shown]
            if fresh:
                shown.update(t["span_text"] for t in fresh)
                block += "\n中文：" + "".join(t["span_text"] for t in fresh)
            blocks.append(block)
        return f"{QA_ANSWER_LEAD}\n\n" + "\n\n".join(blocks)

    if lang == "zh":
        blocks = [f"{c['provision_id']}：{c['span_text']}" for c in citations]
    else:
        blocks = [f"{c['provision_id']}: {c['span_text']}" for c in citations]
    return f"{_extractive_lead(lang)}\n\n" + "\n\n".join(blocks)


def _loads(raw: Any, default: Any) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def _span_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fingerprint(req: ReviewRequest) -> str:
    """请求体指纹：幂等键冲突判定用，不包含幂等键本身。"""
    raw = json.dumps({
        "task_type": req.task_type, "scope": req.scope, "supply": req.supply,
        "profile_version": req.profile_version, "text": req.text,
        "media": req.media, "report_year": req.report_year,
        "answer_lang": req.answer_lang,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _retrieval_query(text: str, acl_scope: list[str] | None) -> Any:
    """延迟导入，避免 reason 模块与 retrieve 形成循环依赖。"""
    from app.retrieve.search import Query

    return Query(text=text, acl_scope=acl_scope, top_k=5)


def _id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000):013d}{secrets.token_hex(6)}"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
