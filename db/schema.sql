-- GIPS 审查 RAG 系统 SQLite DDL（M0 基线，对应 PRD 10.1）
-- 约束说明：
--   1. corpus_type 在库层只允许 A_spec / B_internal，C_ledger 连插入都不可能（AC-11）。
--   2. cot_chains 与 audit_logs 为 append-only，禁用 UPDATE/DELETE 的触发器见 triggers.sql。
--   3. 资金/比率类字段一律 TEXT 存定点数，不用 REAL（契约 2.1）。

PRAGMA foreign_keys = ON;

-- ---------- 用户与角色 ----------
CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT PRIMARY KEY,          -- 用户唯一标识
    name        TEXT NOT NULL,             -- 姓名
    dept        TEXT NOT NULL,             -- 业务线编码，用于 ACL
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'disabled'))
);

CREATE TABLE IF NOT EXISTS role_assignments (
    user_id     TEXT NOT NULL REFERENCES users(user_id),
    role_code   TEXT NOT NULL
                CHECK (role_code IN ('reviewer', 'approver', 'librarian', 'observer', 'admin')),
    scope       TEXT NOT NULL DEFAULT 'global',   -- 角色生效范围
    PRIMARY KEY (user_id, role_code, scope)
);

-- ---------- 语料与条款 ----------
CREATE TABLE IF NOT EXISTS documents (
    doc_id          TEXT NOT NULL,                       -- 业务编号
    version_no      INTEGER NOT NULL,                    -- 版本号，从 1 起
    corpus_type     TEXT NOT NULL
                    CHECK (corpus_type IN ('A_spec', 'B_internal')),  -- C_ledger 禁止入库
    doc_type        TEXT NOT NULL,                       -- 手册 / 制度 / 模板 / 披露
    dept            TEXT NOT NULL,                       -- 归属业务线
    clearance       TEXT NOT NULL
                    CHECK (clearance IN ('internal', 'restricted')),
    effective_from  TEXT NOT NULL,                       -- RFC 3339
    effective_to    TEXT,                                -- 空表示长期有效
    file_hash       TEXT NOT NULL,                       -- sha256(原件字节)
    file_path       TEXT NOT NULL,                       -- 内容寻址路径
    file_size       INTEGER NOT NULL,
    page_count      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'uploaded'
                    CHECK (status IN ('uploaded', 'parsed', 'published', 'failed',
                                      'index_failed', 'deleted')),
    parser          TEXT,                                -- text / ocr / mineru
    text_layer_ratio REAL,                               -- 文本层覆盖率，供解析质量报告
    index_status    TEXT NOT NULL DEFAULT 'pending'
                    CHECK (index_status IN ('pending', 'embedding', 'indexed', 'active',
                                            'failed', 'display_only')),
    index_version   TEXT,                                -- 该版本构建出的 index_version
    active_version  INTEGER,                             -- 当前生效版本（单条 UPDATE 切换）
    deleted_at      TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (doc_id, version_no)
);

CREATE TABLE IF NOT EXISTS provisions (
    provision_id            TEXT NOT NULL,               -- 形如 1.A.4
    version_no              INTEGER NOT NULL,
    section_no              TEXT NOT NULL,               -- 章节号
    obligation              TEXT NOT NULL
                            CHECK (obligation IN ('REQUIREMENT', 'RECOMMENDATION')),
    applicability_condition TEXT,                        -- 前置条件原文
    text_original           TEXT NOT NULL,               -- 条款原文，禁止改写
    text_hash               TEXT NOT NULL,               -- sha256(text_original)
    page_no                 INTEGER NOT NULL,
    effective_from          TEXT NOT NULL,
    effective_to            TEXT,
    PRIMARY KEY (provision_id, version_no)
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id        TEXT PRIMARY KEY,                    -- 32 位十六进制
    doc_id          TEXT NOT NULL,
    version_no      INTEGER NOT NULL,
    provision_id    TEXT,                                -- 可回链的条款编号
    chunk_type      TEXT NOT NULL
                    CHECK (chunk_type IN ('provision', 'section', 'paragraph',
                                          'table_cell', 'semantic')),
    text            TEXT NOT NULL,                       -- 与原件逐字一致的片段
    token_len       INTEGER NOT NULL,                    -- 估算 token 数
    char_start      INTEGER NOT NULL,                    -- 页内 Unicode 码点起点（左闭）
    char_end        INTEGER NOT NULL,                    -- 页内 Unicode 码点终点（右开）
    page_start      INTEGER NOT NULL,
    page_end        INTEGER NOT NULL,
    acl_scope       TEXT NOT NULL,                       -- JSON 数组字符串，空数组合法
    corpus_type     TEXT NOT NULL
                    CHECK (corpus_type IN ('A_spec', 'B_internal')),
    pair_id         TEXT,                                -- 双语 pair 原子
    lang            TEXT NOT NULL CHECK (lang IN ('en', 'zh', 'mixed')),
    index_version   TEXT NOT NULL,
    cleaner_version TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'indexed', 'active', 'superseded', 'failed', 'deleted')),
    FOREIGN KEY (doc_id, version_no) REFERENCES documents(doc_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, version_no);
CREATE INDEX IF NOT EXISTS idx_chunks_provision ON chunks(provision_id);
CREATE INDEX IF NOT EXISTS idx_chunks_status ON chunks(status, index_version);

-- ---------- 映射与画像 ----------
CREATE TABLE IF NOT EXISTS pnp_mappings (
    mapping_id      TEXT PRIMARY KEY,
    pnp_section_id  TEXT NOT NULL,
    provision_id    TEXT NOT NULL,
    mapping_type    TEXT NOT NULL
                    CHECK (mapping_type IN ('full', 'partial', 'none')),
    confirmed_by    TEXT NOT NULL,                        -- 必须是真人确认，不接受系统写入
    confirmed_at    TEXT NOT NULL,
    stale           INTEGER NOT NULL DEFAULT 0           -- 条款版本变更后置 1，不自动重确认
);

CREATE TABLE IF NOT EXISTS firm_profiles (
    profile_version      INTEGER PRIMARY KEY,             -- 单调递增，不原地改
    has_pooled_fund      INTEGER NOT NULL,
    uses_carveout        INTEGER NOT NULL,
    has_illiquid         INTEGER NOT NULL,
    compliance_start_year INTEGER NOT NULL,
    asset_classes        TEXT NOT NULL,                   -- JSON 数组
    effective_from       TEXT NOT NULL
);

-- ---------- 台账 ----------
CREATE TABLE IF NOT EXISTS calc_results (
    calc_id         TEXT PRIMARY KEY,                     -- calc_ + 26 位 ULID
    metric_code     TEXT NOT NULL,
    composite_id    TEXT NOT NULL,
    period          TEXT NOT NULL,
    value           TEXT NOT NULL,                        -- 定点数字符串，禁止浮点
    unit            TEXT NOT NULL,
    engine_version  TEXT NOT NULL,
    source_ref      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_tasks (
    task_id         TEXT PRIMARY KEY,                     -- tsk_ + 26 位 ULID
    -- ingest：异步入库任务（AC-75「写请求进入排队」），与审查任务共用表以复用状态机；
    -- 统计口径必须按 task_type 过滤，不得把 ingest 计入审查任务数（CHG-20261110-11）
    task_type       TEXT NOT NULL CHECK (task_type IN ('qa', 'formal', 'ingest')),
    supply          TEXT NOT NULL
                    CHECK (supply IN ('self_hosted', 'managed_pinned', 'managed')),
    inference_scope TEXT NOT NULL
                    CHECK (inference_scope IN ('local_single_gpu', 'cluster')),
    scope_json      TEXT NOT NULL,
    profile_version INTEGER,
    business_code   TEXT,                                 -- 业务态码（如 RETRIEVAL_EMPTY），终态时由编排器写入；NULL=无业务码
    status          TEXT NOT NULL DEFAULT 'created'
                    CHECK (status IN ('created', 'running', 'succeeded', 'failed',
                                      'pending_retry', 'cancelled')),
    queued_at       TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    -- formal 只允许自建或版本冻结档；managed 只用于探索问答（PRD 2.5.2）
    CHECK (task_type <> 'formal' OR supply IN ('self_hosted', 'managed_pinned')),
    CHECK (task_type <> 'formal' OR profile_version IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS findings (
    finding_id          TEXT PRIMARY KEY,                 -- fnd_ + 26 位 ULID
    task_id             TEXT NOT NULL REFERENCES review_tasks(task_id),
    provision_id        TEXT NOT NULL,
    verdict             TEXT NOT NULL
                        CHECK (verdict IN ('SUFFICIENT', 'PARTIAL', 'GAP', 'INSUFFICIENT_EVIDENCE')),
    verdict_reason      TEXT NOT NULL,
    evidence_refs       TEXT NOT NULL DEFAULT '[]',       -- JSON 数组，元素为 ref_id
    calc_ids            TEXT NOT NULL DEFAULT '[]',
    profile_version     INTEGER NOT NULL,
    model_snapshot_id   TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,
    signoff_status      TEXT NOT NULL DEFAULT 'draft'
                        CHECK (signoff_status IN ('draft', 'pending_signoff', 'signed',
                                                  'finalized', 'disputed', 'superseded', 'stale')),
    signoff_eligibility TEXT NOT NULL
                        CHECK (signoff_eligibility IN ('signable', 'draft_only')),
    row_version         INTEGER NOT NULL DEFAULT 1,       -- 乐观锁
    superseded_by       TEXT,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_findings_provision ON findings(provision_id);

CREATE TABLE IF NOT EXISTS cot_chains (
    chain_id        TEXT PRIMARY KEY,
    -- 回验失败的链也必须保留（AC-40），此时没有 finding，只能挂到 task
    task_id         TEXT REFERENCES review_tasks(task_id),
    finding_id      TEXT REFERENCES findings(finding_id),
    step_no         INTEGER NOT NULL,                     -- 从 1 连续
    step_type       TEXT NOT NULL
                    CHECK (step_type IN ('L', 'A', 'E', 'C', 'J')),
    payload_json    TEXT NOT NULL,                        -- 结构化中间态，不存条款原文
    anchors_json    TEXT NOT NULL,
    validator_result TEXT,                                -- pass / fail / invalidated
    prev_hash       TEXT,                                 -- 哈希链前序值
    self_hash       TEXT,                                 -- sha256(prev_hash + 规范化字段序列)
    created_at      TEXT NOT NULL,
    CHECK (finding_id IS NOT NULL OR task_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_cot_finding ON cot_chains(finding_id, step_no);
CREATE INDEX IF NOT EXISTS idx_cot_task ON cot_chains(task_id, step_no);

CREATE TABLE IF NOT EXISTS evidence_refs (
    ref_id      TEXT PRIMARY KEY,
    doc_id      TEXT NOT NULL,
    version_no  INTEGER NOT NULL,
    page_no     INTEGER NOT NULL,
    span_text   TEXT NOT NULL,                            -- 原文片段，无首尾空白
    span_hash   TEXT NOT NULL,                            -- sha256:<64 hex>
    char_start  INTEGER NOT NULL,                         -- Unicode 码点，左闭
    char_end    INTEGER NOT NULL,                         -- Unicode 码点，右开
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gaps_register (
    gap_id          TEXT PRIMARY KEY,
    finding_id      TEXT NOT NULL REFERENCES findings(finding_id),
    required_action TEXT NOT NULL,                        -- 条款要求的动作
    current_practice TEXT NOT NULL,                       -- 当前做法
    evidence_needed TEXT NOT NULL,                        -- 应补证据类型
    owner           TEXT NOT NULL,                        -- 责任方
    due_date        TEXT,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'in_progress', 'closed', 'accepted'))
);

CREATE TABLE IF NOT EXISTS badcases (
    case_id         TEXT PRIMARY KEY,
    finding_id      TEXT NOT NULL,
    reason_code     TEXT NOT NULL,
    triage_result   TEXT NOT NULL
                    CHECK (triage_result IN ('gold_issue', 'model_issue', 'ui_issue')),
    added_by        TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- ---------- 审计与出域 ----------
CREATE TABLE IF NOT EXISTS audit_logs (
    log_id      TEXT PRIMARY KEY,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL,
    prev_hash   TEXT,
    self_hash   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS egress_logs (
    log_id          TEXT PRIMARY KEY,
    task_id         TEXT,                                 -- 离线构建可为空
    endpoint        TEXT NOT NULL,                        -- 供应商 endpoint
    purpose         TEXT NOT NULL,                        -- ocr / asr / vision / text_query
    egress_class    TEXT NOT NULL
                    CHECK (egress_class IN ('text_query', 'page_image', 'audio', 'vl_image')),
    corpus_type     TEXT NOT NULL,
    doc_id          TEXT,
    version_no      INTEGER,
    page_no         INTEGER,
    crop_box        TEXT,                                 -- JSON {x0,y0,x1,y1}，用于复现
    provider        TEXT,
    state           TEXT NOT NULL
                    CHECK (state IN ('sent', 'failed', 'blocked')),
    payload_hash    TEXT NOT NULL,                        -- 不存原始载荷
    byte_size       INTEGER NOT NULL DEFAULT 0,
    dlp_verdict     TEXT,
    prev_hash       TEXT,
    self_hash       TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_egress_page ON egress_logs(doc_id, version_no, page_no);

-- ---------- 模型与评测 ----------
CREATE TABLE IF NOT EXISTS model_snapshots (
    snapshot_id     TEXT PRIMARY KEY,
    role            TEXT NOT NULL CHECK (role IN ('embed', 'rerank', 'generate', 'ocr', 'asr', 'vision')),
    supply          TEXT NOT NULL
                    CHECK (supply IN ('self_hosted', 'managed_pinned', 'managed')),
    inference_scope TEXT NOT NULL
                    CHECK (inference_scope IN ('local_single_gpu', 'cluster')),
    provider        TEXT NOT NULL,
    model_name      TEXT NOT NULL,
    weight_hash     TEXT NOT NULL,                        -- 权重哈希变更必须新增行
    ctx_limit       INTEGER NOT NULL,                     -- 必填，缺了无法做预算守卫
    deployed_at     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'registered'
                    CHECK (status IN ('registered', 'active', 'retired')),
    CHECK (supply <> 'self_hosted' OR inference_scope = 'local_single_gpu'
           OR inference_scope = 'cluster'),
    CHECK (supply = 'self_hosted' OR inference_scope = 'cluster')
);

-- 模型实测判定一致率：signoff_eligibility 的唯一事实来源（CT-2 硬规则 5）。
-- 与 model_snapshots 分开，因为快照只增不改，一致率会随盲评补测追加。
CREATE TABLE IF NOT EXISTS model_consistency (
    snapshot_id          TEXT PRIMARY KEY REFERENCES model_snapshots(snapshot_id),
    measured_consistency REAL NOT NULL
                         CHECK (measured_consistency >= 0 AND measured_consistency <= 1),
    measured_at          TEXT NOT NULL
);

-- 幂等键：API-4 要求 24 小时内同一 Idempotency-Key 返回同一任务
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idem_key    TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_sets (
    set_id      TEXT NOT NULL,                            -- golden-v1 / dev-v1
    version     TEXT NOT NULL,
    item_count  INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    frozen_at   TEXT,                                     -- 非空即只读
    PRIMARY KEY (set_id, version)
);

CREATE TABLE IF NOT EXISTS eval_results (
    run_id        TEXT PRIMARY KEY,
    set_id        TEXT NOT NULL,
    metric        TEXT NOT NULL,
    value         TEXT NOT NULL,                          -- 定点数字符串
    model_version TEXT NOT NULL,
    config_hash   TEXT NOT NULL,                          -- 缺配置的结果不得作为发布依据
    created_at    TEXT NOT NULL
);
