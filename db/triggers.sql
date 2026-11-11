-- 不可篡改约束的触发器实现（PRD 10.2）
-- 判据：应用账号即使拿到 UPDATE 权限，也必须被数据库层再拦一次（AC-49 提前验证）。

-- cot_chains 只允许追加：任何 UPDATE 都拒绝
CREATE TRIGGER IF NOT EXISTS trg_cot_chains_no_update
BEFORE UPDATE ON cot_chains
BEGIN
    SELECT RAISE(ABORT, 'cot_chains is append-only: UPDATE forbidden');
END;

-- cot_chains 只允许追加：任何 DELETE 都拒绝
CREATE TRIGGER IF NOT EXISTS trg_cot_chains_no_delete
BEFORE DELETE ON cot_chains
BEGIN
    SELECT RAISE(ABORT, 'cot_chains is append-only: DELETE forbidden');
END;

-- findings 结论不覆盖：判定相关字段一经写入不可改，只能新增行并标 superseded_by
CREATE TRIGGER IF NOT EXISTS trg_findings_no_verdict_update
BEFORE UPDATE OF verdict, verdict_reason, evidence_refs, calc_ids,
                 model_snapshot_id, prompt_version, profile_version ON findings
BEGIN
    SELECT RAISE(ABORT, 'findings verdict is immutable: insert a new row instead');
END;

-- findings 不允许物理删除
CREATE TRIGGER IF NOT EXISTS trg_findings_no_delete
BEFORE DELETE ON findings
BEGIN
    SELECT RAISE(ABORT, 'findings is append-only: DELETE forbidden');
END;

-- audit_logs 只允许追加
CREATE TRIGGER IF NOT EXISTS trg_audit_logs_no_update
BEFORE UPDATE ON audit_logs
BEGIN
    SELECT RAISE(ABORT, 'audit_logs is append-only: UPDATE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_logs_no_delete
BEFORE DELETE ON audit_logs
BEGIN
    SELECT RAISE(ABORT, 'audit_logs is append-only: DELETE forbidden');
END;

-- egress_logs 只允许追加：出域记录被改写等于销毁追责依据
CREATE TRIGGER IF NOT EXISTS trg_egress_logs_no_update
BEFORE UPDATE ON egress_logs
BEGIN
    SELECT RAISE(ABORT, 'egress_logs is append-only: UPDATE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_egress_logs_no_delete
BEFORE DELETE ON egress_logs
BEGIN
    SELECT RAISE(ABORT, 'egress_logs is append-only: DELETE forbidden');
END;

-- model_snapshots 只增不改：权重变化必须新增快照（CT-10）
CREATE TRIGGER IF NOT EXISTS trg_model_snapshots_no_update
BEFORE UPDATE ON model_snapshots
BEGIN
    SELECT RAISE(ABORT, 'model_snapshots is append-only: register a new snapshot instead');
END;
