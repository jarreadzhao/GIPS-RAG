# GIPS 合规审查 RAG

面向 GIPS 条款文本的合规审查检索增强系统：把标准与内部制度文档解析成条款树，按条款取证检索，输出带引用回链的判定结论。默认全部本地运行，面向单张 8 GB 显卡、同一时间只服务一个审查任务的场景。

当前版本 `0.1.0-M2`，`GET /healthz` 返回版本号。

## 先读这里：怎么把项目跑起来并调用

顺序是「拿原文 → 起底座 → 装依赖 → 起服务 → 调用」，五步。前四步不做完，第五步一定失败。

### 第 0 步：拿到标准原文

本仓库不附带受版权约束的标准原文，需从官方页面自行获取：

**https://www.gipsstandards.org/standards/gips-standards-for-firms/**

在该页下载两份 PDF，放到仓库根目录下新建的 `standard_paper/`（该目录不入库）：

| 文件 | 用途 | 上传时 `index_mode` |
|---|---|---|
| `2020_gips_standards_firms.pdf` | 英文正本，切分后写入向量库 | `full` |
| `Chinese-Translation-2020-Firms.pdf` | 中文译本，只落 SQLite 供引用对照 | `display_only` |

英文正本是检索与判定的唯一依据；中文译版不参与召回，只在结论里提供对照译文。两份文件的使用条件以官网页面上的许可条款为准。

### 第 1 步：起基础设施

```bash
docker compose up -d
docker compose ps
```

包含 etcd、S3 兼容对象存储（SeaweedFS）、Milvus standalone 与 Redis；首次启动 Milvus 健康检查通过约需 90 秒。数据使用命名卷持久化。

### 第 2 步：装依赖与配置

```bash
pip install -e ".[dev]"
cp .env.example .env
```

`.env.example` 只列键名与默认值，不含任何真实凭据。云 OCR 相关 AK/SK 需自行填写；不启用 OCR 时保持默认即可。

### 第 3 步：启动后端

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8011
```

启动顺序：配置校验 → SQLite 建表与触发器 → 回收崩溃遗留的中间态索引 → 组装显存池。模型惰性加载，启动本身不占显存，首个检索请求触发冷加载。

健康检查：

```bash
curl http://127.0.0.1:8011/healthz
```

```json
{"status": "ok", "version": "0.1.0-M2", "llm_backend": "fake", "resident_models": ["embed", "rerank"]}
```

`resident_models` 是当前常驻显存的槽位，可与 `nvidia-smi` 交叉验证——8 GB 卡上不应同时出现 `generate` 与 `embed/rerank`。

### 第 4 步：调用

两种方式，任选其一。项目没有 CLI，对外只有 HTTP 接口。

**方式 A：浏览器操作台**

```bash
python -m http.server 8765 --bind 127.0.0.1 --directory frontend
```

访问 `http://127.0.0.1:8765/`。操作台会自动带上 `X-Role: reviewer`、`X-Dept: compliance` 与 `Idempotency-Key`。

**方式 B：直接调 HTTP 接口**

| 方法 | 路径 | 用途 | 必需请求头 |
|---|---|---|---|
| GET | `/healthz` | 存活与版本 | — |
| POST | `/documents` | 上传 PDF，返回 `202` + `task_id` | — |
| GET | `/documents/{doc_id}` | 文档与索引状态 | — |
| POST | `/reviews` | 发起条款审查 | `Idempotency-Key`、`X-Role`、`X-Dept` |
| GET | `/reviews/{task_id}` | 取审查结果 | `X-Role`、`X-Dept` |
| GET | `/cot/{finding_id}` | 取该条结论的推理链 | `X-Role`、`X-Dept` |
| GET | `/provisions/search?q=1.A.4` | 按条款号检索 | `X-Role`、`X-Dept` |
| POST | `/retrieval/test` | 检索调试 | `X-Role`、`X-Dept` |

上传英文正本（建索引）：

```bash
curl -X POST http://127.0.0.1:8011/documents \
  -F "file=@standard_paper/2020_gips_standards_firms.pdf" \
  -F "doc_id=gips2020_en" -F "version_no=1" \
  -F "corpus_type=A_spec" -F "doc_type=制度" \
  -F "dept=compliance" -F "clearance=internal" \
  -F "effective_from=2020-01-01T00:00:00Z" -F "index_mode=full"
```

上传中文译本（只落库，不建索引）：

```bash
curl -X POST http://127.0.0.1:8011/documents \
  -F "file=@standard_paper/Chinese-Translation-2020-Firms.pdf" \
  -F "doc_id=gips2020_zh" -F "version_no=1" \
  -F "corpus_type=A_spec" -F "doc_type=制度" \
  -F "dept=compliance" -F "clearance=internal" \
  -F "effective_from=2020-01-01T00:00:00Z" -F "index_mode=display_only"
```

`corpus_type` 只能是 `A_spec`（外部标准）或 `B_internal`（内部制度）；`clearance` 只能是 `internal` 或 `restricted`；`effective_from` 用 RFC 3339。缺项会一次性列全，不用逐条试错。

发起一次正式审查：

```bash
curl -X POST http://127.0.0.1:8011/reviews \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: 0f8f1d1e-6a1b-4c47-9d2e-2b7f3c5a1d40" \
  -H "X-Role: reviewer" -H "X-Dept: compliance" \
  -d '{"task_type":"formal","scope":["1.A.4"],"text":"该组合是否满足 composite 定义？","answer_lang":"zh"}'
```

取结果：

```bash
curl -H "X-Role: reviewer" -H "X-Dept: compliance" \
  http://127.0.0.1:8011/reviews/<task_id>
```

`task_type=formal` 要求 `X-Role` 属于 `reviewer` / `librarian` / `approver` / `admin`；`task_type=qa` 无此限制。`Idempotency-Key` 必填，重放同一键不会重复建任务。结论带 `status` 字段，为 `draft` 时表示证据不足、不可签署。

### 第 5 步：接入模型权重（可选）

不接权重也能跑通全链路——`GIPS_LLM_BACKEND` 默认 `fake`，用桩模型返回结构合法的结果。需要真实检索与生成时，把权重放到 `models/` 下：

```
models/embed/        bge-m3
models/rerank/       bge-reranker-v2-m3
models/generate/     qwen3-8b-q4_k_m.gguf
```

权重文件约 9.5 GB，不入库，需自行从模型发布页下载。`llama-cpp-python` 需要带 CUDA 后端的 wheel，安装方式见 `scripts/install_llama_cpp.ps1`。

## 它解决什么问题

- **条款级取证**，不是全文问答：先定位到具体条款，再围绕条款取证与判定。
- **引用可回链**：结论里的每条证据都能追溯到原文页码与段落。
- **判定可降级**：证据或质量不达标时输出 `draft` 结论，标记为不可签署，而不是硬给一个看似确定的答案。
- **数据不出域**：检索、重排、生成全部本地推理；仅可选的云 OCR 需要出网，且只经由独立出域网关进程。

## 硬约束

这些约束是设计前提，不是待优化的项：

| 约束 | 取值 | 影响 |
|---|---|---|
| 显存 | 单卡 8 GB，峰值上限 7.5 GB | 生成模型只能用 8B 量化版，且与向量模型分时独占 |
| 上下文 | 4096 token | 长条款必须切分取证，不能整篇塞入 |
| 并发 | 1 | 单机单任务串行，不做多路复用 |
| 切分 | 按条款结构切分，重叠为 0 | 禁止固定长度带重叠切分，避免跨条款串味 |
| 生成后端 | 默认 `fake` 桩 | 未就位模型权重也能跑通全链路 |

## 技术栈

| 层 | 选型 |
|---|---|
| 服务 | FastAPI + Uvicorn + Pydantic v2 |
| 元数据 | SQLite（WAL）+ 触发器与视图 |
| 向量库 | Milvus 2.4.6 standalone（etcd + S3 兼容对象存储） |
| 缓存 / 队列 | Redis 7 |
| 解析 | PyMuPDF，文本层与 OCR 双路由 |
| 向量与重排 | bge-m3、bge-reranker-v2-m3 |
| 生成 | Qwen3-8B GGUF，经 llama-cpp-python 推理 |
| 前端 | 原生 HTML / CSS / JS 静态操作台 |

## 目录结构

| 路径 | 功能 |
|---|---|
| `app/` | 后端服务本体。`main.py` 装配应用，`config.py` 读环境变量，`errors.py` 定义统一错误码 |
| `app/api/` | HTTP 接口层：`documents.py` 上传受理，`reviews.py` 审查与推理链，`retrieval.py` 条款检索与调试 |
| `app/core/` | 配置校验、日志、SQLite 连接与事务、显存池调度（embed / rerank / generate 互斥） |
| `app/parse/` | PDF 解析：文本层优先，缺文本层时走可选云 OCR，输出页级文本与坐标 |
| `app/index/` | 条款结构切分、元数据校验、向量写入与索引状态机 |
| `app/retrieve/` | 条款定位、混合召回、重排，以及基于角色的检索范围（ACL） |
| `app/reason/` | 判定编排：证据组织、CoT 调用、结论与引用回链组装、`draft` 降级 |
| `app/ingest/` | 异步入库写队列，串行处理上传后的解析、切分与建索引 |
| `app/eval/` | 评测钩子，供评测集复跑时计算召回与引用命中 |
| `config/` | 运行配置，如 `index-v1.yaml` 的索引与切分参数 |
| `db/` | SQLite 三件套：`schema.sql` 建表、`triggers.sql` 状态机约束、`views.sql` 查询视图 |
| `prompts/` | CoT 判定提示词模板，与 `app/reason/` 配套 |
| `eval/` | `dev/` 开发集与 `golden/` 金标集（`*.jsonl`），以及污染检查清单 |
| `frontend/` | 原生静态操作台：`index.html` + `js/api.js` 请求封装 + `js/pages/` 三个页面 |
| `scripts/` | 部署与运维脚本：起停（`dev_up.ps1`）、抓包、显存探测、llama.cpp 安装、索引重建、自评测 |
| `src/` | 一次性数据分析脚本（论文抽取、PDF 探针），不参与服务运行 |
| `tests/` | 单元与集成测试；标 `gpu` 的用例需真实加载模型 |
| `v1-spec/` | v1 规格包：`spec/` 四件套、`changes/` 变更单、`tools/` 机检脚本、`trae-instructions/` 实施清单 |
| `docs/` | 运维手册、标注规范、条款树、解析与切分报告、显存基线 |
| `reports/` | 阶段性抓包、审查走查与验收报告 |
| `.trae/rules/` | 项目协作规则，约束 AI 工具在本仓库内的行为 |
| `.publish-safety/` | 发布白名单，声明哪些路径允许推到公开仓库 |
| `pyproject.toml` | 依赖与服务元信息，`pip install -e ".[dev]"` 的入口 |
| `docker-compose.yml` | etcd / SeaweedFS / Milvus / Redis 的底座编排 |
| `.env.example` | 环境变量模板，只含键名与默认值，不含凭据 |
| `README.md` | 本文件：调用步骤、硬约束与已知限制 |
| `.gitignore` | 排除 `models/`、`var/`、`work/` 等本地产物 |
| `.gitattributes` | 统一换行符为 LF，避免跨平台差异 |

`models/`、`var/`、`standard_paper/`、`work/` 是本地产物目录，不入库，需自行创建。

## 已知限制

- 仓库只含源代码与规格文档，不含模型权重、向量库数据与标准原文。克隆后必须补齐这三样才能真实检索：模型见第 5 步，向量库由上传 PDF 触发重建，原文见第 0 步。
- 单卡 8 GB 决定了生成模型规模与 4k 上下文上限，长文档只能分条款取证，不支持整篇长上下文推理。
- 并发为 1，多任务需排队；不适合作为多人在线服务直接暴露。
- 云 OCR 需要供应商凭据与零留存条款，未配置时只能走文本层解析。
- 评测集规模有限，边界条款的判定质量仍需资深审查员抽检。

## 测试

```bash
pip install -e ".[dev]"
python -m pytest -q
```

标了 `gpu` 的用例会真实加载模型并占用显存，未接 GPU 时可跳过。索引重建后可用 `eval/golden/golden-v1.jsonl` 复跑，比对召回与引用是否与既有基线一致。

## 规格文档

`v1-spec/` 是 v1 的完整规格包，派生链单向：`R → CT → API → AC`，每条验收都标注来源需求。

```bash
python v1-spec/tools/spec_link.py --root v1-spec       # 四件套跨文件联检
python v1-spec/tools/prd_lint.py v1-spec/spec/PRD.md   # PRD 单文件结构细检
```

阅读顺序见 `v1-spec/README.md`；日常运维见 `docs/运维手册.md`。

## 许可证

本仓库暂未附带开源许可证文件。在补充许可证之前，请勿直接复制、分发或商用本仓库内容。
