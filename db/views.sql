-- 检索侧只读视图（M1 / R-4）
-- 目的：结构化通道只能看到白名单视图，视图本身不含 C 类台账表。
-- 注意：视图会读 chunks/documents 基表，检索连接的 authorizer 必须放行这两张表，
-- 但 calc_results / firm_profiles 等台账表一律拒绝。

-- 条款索引视图：一行一个条款（同一文档版本内聚合其全部 chunk）
CREATE VIEW IF NOT EXISTS v_provision_index AS
SELECT c.provision_id              AS provision_id,
       c.doc_id                    AS doc_id,
       c.version_no                AS version_no,
       d.dept                      AS dept,
       d.clearance                 AS clearance,
       d.corpus_type               AS corpus_type,
       d.effective_from            AS effective_from,
       d.effective_to              AS effective_to,
       c.lang                      AS lang,
       c.index_version             AS index_version,
       MIN(c.page_start)           AS page_start,
       MAX(c.page_end)             AS page_end,
       COUNT(*)                    AS chunk_count,
       MIN(substr(c.text, 1, 200)) AS snippet
FROM chunks c
JOIN documents d ON d.doc_id = c.doc_id AND d.version_no = c.version_no
WHERE c.status = 'active' AND c.provision_id IS NOT NULL AND c.provision_id <> ''
GROUP BY c.provision_id, c.doc_id, c.version_no, d.dept, d.clearance,
         d.corpus_type, d.effective_from, d.effective_to, c.lang, c.index_version;

-- chunk 元数据视图：不含全文，只暴露定位与标签
CREATE VIEW IF NOT EXISTS v_chunk_meta AS
SELECT c.chunk_id       AS chunk_id,
       c.doc_id         AS doc_id,
       c.version_no     AS version_no,
       c.provision_id   AS provision_id,
       c.chunk_type     AS chunk_type,
       c.lang           AS lang,
       c.page_start     AS page_start,
       c.page_end       AS page_end,
       c.char_start     AS char_start,
       c.char_end       AS char_end,
       c.corpus_type    AS corpus_type,
       c.acl_scope      AS acl_scope,
       c.index_version  AS index_version,
       c.status         AS status,
       d.dept           AS dept,
       d.clearance      AS clearance,
       d.doc_type       AS doc_type,
       d.effective_from AS effective_from,
       d.effective_to   AS effective_to
FROM chunks c
JOIN documents d ON d.doc_id = c.doc_id AND d.version_no = c.version_no
WHERE c.status = 'active';

-- 文档元数据视图
CREATE VIEW IF NOT EXISTS v_document_meta AS
SELECT doc_id, version_no, corpus_type, doc_type, dept, clearance,
       effective_from, effective_to, status, index_status, index_version
FROM documents
WHERE status <> 'deleted';

-- P&P 映射视图
CREATE VIEW IF NOT EXISTS v_mappings AS
SELECT mapping_id, pnp_section_id, provision_id, mapping_type,
       confirmed_by, confirmed_at, stale
FROM pnp_mappings;
