# 代码 ↔ 规格对照表

用途：`spec_sync.py` 用它回答一个问题 —— **你改的这个代码文件，挂在哪条规格上**。

它不是权威映射（真正的权威是 PRD 正文与契约），而是一张索引：改了左侧，右侧那几条就该被翻一遍，确认是「代码对、规格旧」还是「代码错」。路径 glob 相对仓库根。

漏写的条目只会少提示，不会误拦；写错的条目会让门禁把无关改动算进影响范围。**新增模块时同步加一行**，否则新模块的改动等于没有规格归属。

## 后端

| 路径 glob | 规格编号 | 说明 |
|---|---|---|
| `app/main.py` | R-1, R-6 | 单体入口、只读降级开关 |
| `app/config.py` | R-1, R-2 | 配置装配；出域开关在此 |
| `app/errors.py` | R-45 | 错误码与正文分离 |
| `app/core/logging.py` | R-5, R-38 | 全链路追踪自托管 |
| `app/core/db.py` | R-41, R-43 | SQLite 单写者、哈希链、台账隔离 |
| `app/core/egress.py` | R-2, R-42, R-44, R-47, CT-6, CT-11 | 唯一出域网关；缺 egress_class 即拒 |
| `app/core/llm.py` | R-3, R-48, CT-2 | 供给档校验；draft 资格；禁截断 |
| `app/core/embed.py` | R-13, R-46, CT-6 | bge-m3 单套多语；分时独占 |
| `app/core/rerank.py` | R-26, R-46 | 条款级重排；分时独占 |
| `app/core/gpu_pool.py` | R-46, AC-12A, AC-12B | 8 GB 三模型分时独占；OOM 即 P0 |
| `app/api/documents.py` | R-14, R-16, R-17, CT-9 | 上传幂等、版本链、质量闸门 |
| `app/api/retrieval.py` | R-8, R-18, R-23, R-25, CT-1, CT-5, CT-7 | 三通道检索、ACL、缓存 |
| `app/api/reviews.py` | R-20, R-37, R-45, R-49, CT-1, CT-2, CT-8 | 审查任务、时延、签署；`answer_lang` 校验 |
| `app/parse/router.py` | R-11 | 文本层检测分派；有文本层不送 OCR |
| `app/parse/pdf_text.py` | R-11, R-17 | 按坐标分栏；空块率 |
| `app/parse/ocr/aliyun.py` | R-47, CT-11, AC-12C | 云端 OCR；审批锚定 |
| `app/parse/ocr/tencent.py` | R-47, CT-11 | 第二供应商 |
| `app/parse/ocr/crop.py` | R-47, AC-12D | 裁边预处理 |
| `app/parse/ocr/base.py` | R-47, CT-11 | Provider 归一 |
| `app/parse/chunker.py` | R-12, R-13 | 条款边界切分；pair 为原子 |
| `app/parse/cleaner.py` | R-17 | 清洗与空块 |
| `app/parse/bilingual.py` | R-13 | 段落对齐、pair 置信度 |
| `app/index/builder.py` | R-13, R-15, CT-4, AC-5, AC-9, AC-10 | 两阶段提交；计数一致才 active；`display_only` 显示层只落 SQLite |
| `app/index/pairing.py` | R-13, AC-5 | 中英块按条款配对；等量 1:1、不等量整条共用 |
| `app/index/tagging.py` | R-10, R-14 | corpus_type / 密级打标 |
| `app/ingest/worker.py` | R-6, AC-75, CT-9 | 异步入库写队列；云通道关闭时写请求排队并落失败码 |
| `app/index/milvus_schema.py` | R-25, R-43 | 标量字段前置；台账无路径 |
| `app/retrieve/search.py` | R-23, R-27 | 三通道编排、缓存查/写 |
| `app/retrieve/vector.py` | R-23, R-25 | 稠密检索 + ACL 过滤 |
| `app/retrieve/fusion.py` | R-23 | RRF 融合；跨语言关稀疏 |
| `app/retrieve/acl.py` | R-8, R-25, AC-15 | 候选集前注入，fail-closed |
| `app/retrieve/structured.py` | R-24, CT-7 | 白名单、200 行、5s、只读 |
| `app/retrieve/types.py` | R-28 | 三类「没有答案」 |
| `app/reason/intent.py` | R-21, R-22 | 三出口；改写不改条款号 |
| `app/reason/orchestrator.py` | R-20, R-29, R-37, R-49, CT-2 | 五段式编排；三段分轮；答案语种单一化 |
| `app/reason/prompts/cot_v1.py` | R-29, R-32 | 结构化输出；占位符 |
| `app/reason/anchor.py` | R-29, R-34 | 锚点抽取与落位 |
| `app/reason/verifier.py` | R-30, R-34, R-35, R-36, CT-3 | 字节级反向校验；失败整链作废 |
| `app/reason/eligibility.py` | R-31 | 适用性三态、终止优先 |
| `app/reason/guard.py` | R-40 | 入模前 / 出模后过滤 |
| `app/eval/dataset.py` | R-19, AC-13 | golden / dev 分离 |
| `app/eval/labeler.py` | R-19, AC-16, AC-24 | AI 初标三取二；自一致率 |
| `app/eval/metrics.py` | R-19 | Recall@5 / MRR |
| `app/eval/runner.py` | R-18, R-19 | 测试台、配置对比 |
| `db/schema.sql` | R-41, R-43, CT-8 | 台账表结构 |
| `db/triggers.sql` | R-41 | 禁 UPDATE / DELETE |
| `db/views.sql` | R-24, CT-7 | 结构化只读视图 |
| `config/index-v1.yaml` | R-12, R-15 | 切分与索引参数 |
| `prompts/cot-v1.md` | R-29, R-32 | 提示词正文 |

## 实验与脚本

| 路径 glob | 规格编号 | 说明 |
|---|---|---|
| `src/extract.py` | R-11 | 分栏抽取（已被 app/parse 接管） |
| `src/chunk.py` | R-12 | 切分对比 |
| `src/eval_retrieval.py` | R-19 | 离线检索评测 |
| `src/build_pairs.py` | R-13 | 双语对齐 |
| `scripts/pair_bilingual.py` | R-13, AC-5 | 回写 `chunks.pair_id`，可重入 |
| `scripts/ac79_ac80_audit.py` | R-34, R-35, AC-79, AC-80 | 引用逐条回验与答案越界扫描 |
| `scripts/load_test_qa.py` | R-37, AC-71, AC-71A, AC-71B, AC-72 | 并发压测：端到端时延、错误率、队列位次、吞吐 |
| `scripts/soak_test.py` | R-37, AC-74 | 长时间浸泡：RSS 与句柄采样 |
| `scripts/rebuild_index.py` | R-39, AC-73 | 删集合 / 由原件重建 / 逐阶段计时与核数 |
| `scripts/*` | — | M0 分析脚本，不动线上行为 |

## 测试

| 路径 glob | 规格编号 | 说明 |
|---|---|---|
| `tests/test_ac_*.py` | 文件名中的 AC | 一个文件对应一条验收标准 |
| `tests/test_gpu_pool.py` | R-46, AC-12A | 分时独占 |
| `tests/test_anchor_roundtrip.py` | R-34, R-35 | 锚点回环 |
| `tests/test_eval_guards.py` | R-19 | 评测守卫 |
| `tests/test_m2_review.py` | R-29…R-33, R-37, R-49 | 单条款审查闭环；QA 结构化引用与按语种单侧引用 |
| `tests/test_bilingual_pairing.py` | R-13, AC-5 | 配对规则：等量 1:1、不等量整条、无条款不配对 |
| `tests/test_m5_queue_observability.py` | AC-71A | 队列位次可观测且不被长操作阻塞 |

## 前端

| 路径 glob | 规格编号 | 说明 |
|---|---|---|
| `frontend/js/i18n.js` | R-49 | 中英界面词典与语言状态；切换后整页重载 |
| `frontend/js/pages/chat.js` | R-49, R-28, AC-83 | 问答台；语种切换与单语种引用渲染 |
| `frontend/js/api.js` | R-49 | `answer_lang` 透传 |

---

## 变更单里的「代码变更追溯」节格式

每张动了代码的 CHG 必须有这一节。三行即可，缺字段门禁直接 FAIL。

```markdown
## 代码变更追溯

| 项 | 内容 |
|---|---|
| 规格影响 | R-46, AC-12A（或写：无） |
| 代码文件 | app/core/gpu_pool.py, tests/test_gpu_pool.py |
| 说明 | 显存池改为按组独占，OOM 时整组重建而非单模型重载 |
```

三条规矩：

1. **「规格影响」写编号，不要写「详见上文」。** 门禁按编号判断，写不写编号决定它拦不拦。
2. **写「无」可以，但要能说出口。** 纯重构、加日志、改类型注解 → 写「无」。改了行为却说无，等于自己给自己签免责声明。
3. **「代码文件」要真实。** 门禁会核对它是否出现在本次 diff 里 —— 事后补编的路径会被抓出来。
