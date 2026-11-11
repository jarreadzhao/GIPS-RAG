# 五段式审查提示词 cot-v1

- 代码实现：`app/reason/prompts/cot_v1.py`
- 版本标识：`cot-v1`
- 内容哈希：`sha256:052fe6031f7904fa08e0c3b47728690fc47949886946f18efd72410082dbacfa`
  （`prompt_hash()` 对三轮模板原文计算，不含运行时 payload）

## 1. 为什么分三轮

单卡 4096 上下文的硬预算装不下「条款原文 + 机构画像 + 全部候选证据 + 整条链」。
固定长度截断被产品禁止（AC-47A），因此按环节切分，每轮只做一件事，
轮间只传结构化中间态。

| 轮次 | 步骤 | 输入 | 输出 |
| --- | --- | --- | --- |
| 轮 1 | L 定位、A 适用性 | 条款编号、报告年度、条款元数据、画像快照 | `locate`、`applicability`（APPLIES / NOT_APPLIES / UNKNOWN） |
| 轮 2 | E 抽取、C 比对 | 条款编号、适用性、候选证据引用（无原文） | `evidence_refs`（只含 `ref_id`）、`comparisons`（match / mismatch / missing） |
| 轮 3 | J 判定 | 条款编号、比对结果、证据引用编号 | `verdict`（SUFFICIENT / PARTIAL / GAP / INSUFFICIENT_EVIDENCE）、理由、缺口清单 |

## 2. 轮间传输约束（AC-47B）

- 轮 2、轮 3 的 payload 不含 `text_original`、`span_text` 或任何条款/证据原文。
- 证据由系统按候选偏移从原件切片，模型只返回 `ref_id`；模型不得编造 `span_text`
  或改写偏移。
- 每步必须带 `anchors`，缺锚点直接 `ANCHOR_INVALID`（AC-35）。

## 3. 提示词里的硬约束

1. 不得改写条款编号。
2. 不得输出金额、比例、小数；需要数值时只能写 `calc://{calc_id}`。
3. 不得使用「通常 / 一般来说 / 行业惯例 / 实践中多数机构 / 类似标准要求」。
4. NOT_APPLIES 后不得附带「即使适用也可能不满足」类提示（AC-42）。
5. 不得输出「已验证 / 合规通过 / 保证符合」等效力性表述。

提示词只是第一道，出模后还有独立守卫：

| 守卫 | 位置 | 失败码 |
| --- | --- | --- |
| 入模注入检测 | `app/reason/guard.py::assert_no_injection` | `VALIDATION_FAILED` |
| 数值扫描 | `app/reason/guard.py::assert_numbers_allowed` | `CALC_NOT_ALLOWED` |
| 效力性表述过滤 | `app/reason/guard.py::filter_efficacy` | 替换为「【内部自查发现，非合规结论】」，原文不落库 |

## 4. 生成后端适配

- 真实后端：`llama_cpp` + Qwen3-8B-Instruct Q4_K_M（`models/generate/qwen3-8b-q4_k_m.gguf`）。
- Qwen3 默认先输出思考内容，会把 JSON 挤掉；`llama-cpp-python` 当前不支持
  `chat_template_kwargs`，因此在提示词末尾追加官方 `/no_think` 软开关。
- 温度固定 0，保证同一输入可复现；`max_tokens=700`。
