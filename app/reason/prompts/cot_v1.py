"""五段式 L-A-E-C-J 提示词 v1（T-2-01 / AC-47B）。

设计约束：
    1. 4k 上下文装不下整条链，按三轮分段：
       轮 1 定位 + 适用性（L、A）
       轮 2 事实抽取 + 比对（E、C）
       轮 3 判定 + 理由（J）
    2. 轮间只传结构化中间态（provision_id、页码、码点区间、比对状态），
       不传条款原文与证据原文（AC-47B）。
    3. 每段都必须输出 anchors；缺 anchors 直接非法（AC-35）。
    4. 禁用语写进提示词只是第一道；出模后还有独立过滤（AC-46）。

本模块只负责拼提示词，不做任何业务判断。
"""
from __future__ import annotations

import hashlib
import json

PROMPT_VERSION = "cot-v1"

# PRD 6.3 机制三：检索不到时不得用先验知识补齐
FORBIDDEN_PHRASES = (
    "通常",
    "一般来说",
    "行业惯例",
    "实践中多数机构",
    "类似标准要求",
)

# PRD 9.3 / R-40：效力性表述一律不得出现在输出中
EFFICACY_PHRASES = (
    "已验证",
    "合规通过",
    "保证符合",
    "保证合规",
    "符合 GIPS 要求",
    "符合GIPS要求",
)

# 入模前注入检测（AC-47）
INJECTION_MARKERS = (
    "忽略以上指令",
    "忽略之前",
    "忽略上述",
    "直接判合规",
    "覆盖系统提示",
    "ignore previous",
    "ignore all prior",
    "disregard above",
    "you are now",
)

# NOT_APPLIES 后禁止附带的提示性缺口（AC-42）
HINT_PHRASES = (
    "即使适用",
    "也可能不满足",
    "顺带",
    "顺便提",
    "仅供参考",
)

ROUND1_TEMPLATE = """你是 GIPS 合规自查系统的“定位与适用性”步骤。
只输出一个 JSON 对象，不要输出解释文字。

输入（JSON）：
{payload}

请完成：
L 定位：确认 provision_id 是否存在、生效期是否覆盖报告期。anchors 至少包含
  {{"type":"provision","provision_id":"...","page_no":1}}。
A 适用性：读 profile 快照，按条款的 applicability_condition 求值，输出
  APPLIES / NOT_APPLIES / UNKNOWN 三态之一。

输出 JSON 结构：
{{
  "locate": {{"provision_id": "...", "page_no": 1, "anchors": [...]}},
  "applicability": {{"value": "APPLIES|NOT_APPLIES|UNKNOWN", "reason": "...",
                     "missing_profile_fields": [], "anchors": [...]}}
}}

约束：不得改写条款编号；不得输出任何金额、比例或小数；不得使用
“通常 / 行业惯例 / 类似标准要求”等措辞。
"""

ROUND2_TEMPLATE = """你是 GIPS 合规自查系统的“证据抽取与比对”步骤。
只输出一个 JSON 对象，不要输出解释文字。

输入只含结构化中间态与候选证据引用，不含条款原文：
{payload}

请完成：
E 抽取：对每项要求选出候选证据，evidence_refs 元素只写
  {{"ref_id":"候选中的 ref_id"}}；系统会按候选偏移从原件切片并回验，
  你不得自行编造 span_text、不得改写候选偏移。
C 比对：逐项输出 status，取值 match / mismatch / missing，并引用 ref_id。

输出 JSON 结构：
{{
  "evidence_refs": [{{"ref_id":"..."}}],
  "comparisons": [{{"requirement":"...", "status":"match|mismatch|missing",
                    "evidence_id":"..."}}]
}}

约束：不得输出任何未经原件支持的片段；不得输出金额、比例或小数。
"""

ROUND3_TEMPLATE = """你是 GIPS 合规自查系统的“判定与理由”步骤。
只输出一个 JSON 对象，不要输出解释文字。

输入只含结构化比对结果，不含条款与证据原文：
{payload}

请给出四态之一：SUFFICIENT / PARTIAL / GAP / INSUFFICIENT_EVIDENCE。
规则：
- PARTIAL 必须能拆出具体缺失项；拆不出就降级为 GAP。
- INSUFFICIENT_EVIDENCE 必须在 gaps 中写明 evidence_needed 与 owner。
- 禁止输出“已验证 / 合规通过 / 保证符合”等效力性表述。
- 禁止输出任何金额、比例或小数；需要数值时只写 calc://{{calc_id}}。

输出 JSON 结构：
{{
  "verdict": "...",
  "verdict_reason": "...",
  "gaps": [{{"required_action":"...","current_practice":"...",
             "evidence_needed":"...","owner":"..."}}],
  "anchors": [{{"type":"provision","provision_id":"...","page_no":1}}]
}}
"""


def round1_prompt(payload: dict) -> str:
    """轮 1：定位 + 适用性。"""
    return ROUND1_TEMPLATE.format(payload=_dumps(payload))


def round2_prompt(payload: dict) -> str:
    """轮 2：抽取 + 比对。"""
    return ROUND2_TEMPLATE.format(payload=_dumps(payload))


def round3_prompt(payload: dict) -> str:
    """轮 3：判定 + 理由。"""
    return ROUND3_TEMPLATE.format(payload=_dumps(payload))


def prompt_hash() -> str:
    """三轮提示词的内容哈希，写入 finding 的 prompt_version 旁证。"""
    joined = ROUND1_TEMPLATE + ROUND2_TEMPLATE + ROUND3_TEMPLATE
    return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _dumps(payload: dict) -> str:
    """稳定序列化，避免键序变化导致提示词哈希漂移。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
