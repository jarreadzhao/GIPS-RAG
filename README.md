# GIPS 合规审查 RAG

面向 GIPS 条款文本的合规审查检索增强系统：把标准与内部制度文档解析成条款树，按条款取证检索，输出带引用回链的判定结论。默认全部本地运行，面向单张 8 GB 显卡、同一时间只服务一个审查任务的场景。

当前版本 `0.1.0-M2`，`GET /healthz` 返回版本号。

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

```
app/            后端服务（api / core / parse / index / retrieve / reason / ledger / ingest / eval）
config/         运行配置（如索引参数 index-v1.yaml）
db/             SQLite 建表、触发器与视图
prompts/        CoT 判定提示词
eval/           评测集（dev / golden）与污染检查清单
frontend/       静态操作台
scripts/        部署与运维脚本（PowerShell / Python）
src/            一次性数据分析脚本
tests/          单元与集成测试
v1-spec/        v1 规格包：PRD、契约、接口、验收标准与机检脚本
docs/           运维手册、标注规范与各类评估报告
reports/        阶段性审查与验收报告
```

`models/`、`var/`、`standard_paper/`、`work/` 是本地产物目录，不入库。

## 快速开始

### 1. 启动基础设施

```bash
docker compose up -d
docker compose ps
```

包含 etcd、S3 兼容对象存储（SeaweedFS）、Milvus standalone 与 Redis；首次启动 Milvus 健康检查通过约需 90 秒。数据使用命名卷持久化。

### 2. 准备配置

```bash
cp .env.example .env
```

`.env.example` 只列键名与默认值，不含任何真实凭据。云 OCR 相关 AK/SK 需自行填写；不启用 OCR 时保持默认即可。

### 3. 启动后端

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8011
```

启动顺序：配置校验 → SQLite 建表与触发器 → 回收崩溃遗留的中间态索引 → 组装显存池。模型惰性加载，启动本身不占显存，首个检索请求触发冷加载。

### 4. 打开操作台

```bash
python -m http.server 8765 --bind 127.0.0.1 --directory frontend
```

浏览器访问 `http://127.0.0.1:8765/`。

### 5. 健康检查

```bash
curl http://127.0.0.1:8011/healthz
```

返回示例：

```json
{"status": "ok", "version": "0.1.0-M2", "llm_backend": "fake", "resident_models": ["embed", "rerank"]}
```

`resident_models` 是当前常驻显存的槽位，可与 `nvidia-smi` 交叉验证——8 GB 卡上不应同时出现 `generate` 与 `embed/rerank`。

### 6. 接入模型权重（可选）

把权重放到 `models/` 下，未就位时保持 `GIPS_LLM_BACKEND=fake`：

```
models/embed/        bge-m3
models/rerank/       bge-reranker-v2-m3
models/generate/     qwen3-8b-q4_k_m.gguf
```

`llama-cpp-python` 需要带 CUDA 后端的 wheel，安装方式见 `scripts/install_llama_cpp.ps1`。

## 测试

```bash
pip install -e ".[dev]"
python -m pytest -q
```

标了 `gpu` 的用例会真实加载模型并占用显存，未接 GPU 时可跳过。

## 规格文档

`v1-spec/` 是 v1 的完整规格包，派生链单向：`R → CT → API → AC`，每条验收都标注来源需求。

```bash
python v1-spec/tools/spec_link.py --root v1-spec       # 四件套跨文件联检
python v1-spec/tools/prd_lint.py v1-spec/spec/PRD.md   # PRD 单文件结构细检
```

阅读顺序见 `v1-spec/README.md`；日常运维见 `docs/运维手册.md`。

## 已知限制

- 单卡 8 GB 决定了生成模型规模与 4k 上下文上限，长文档只能分条款取证，不支持整篇长上下文推理。
- 并发为 1，多任务需排队；不适合作为多人在线服务直接暴露。
- 云 OCR 需要供应商凭据与零留存条款，未配置时只能走文本层解析。
- 评测集规模有限，边界条款的判定质量仍需资深审查员抽检。

## 许可证

本仓库暂未附带开源许可证文件。在补充许可证之前，请勿直接复制、分发或商用本仓库内容。
