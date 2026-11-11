"""签署资格派生（T-2-09 / AC-47C、AC-47D）。

`signoff_eligibility` 永远由系统算，客户端传什么都忽略：
    local_single_gpu 且实测一致率 < 0.85 -> draft_only
    local_single_gpu 且一致率 >= 0.85    -> signable
    未登记一致率                          -> draft_only（宁严勿松）

一致率只认 model_consistency 表；没有实测就当作不具备签署资格，
不允许用「模型自报」「配置声明」代替。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from app.config import Settings
from app.core.db import Database
from app.core.logging import get_logger
from app.errors import AppError

CONSISTENCY_THRESHOLD = 0.85


def ensure_generate_snapshot(db: Database, settings: Settings) -> dict[str, Any]:
    """返回当前 generate 快照；没有则登记一条 self_hosted 快照。"""
    row = db.fetch_one(
        "SELECT * FROM model_snapshots WHERE role='generate' AND status='active' "
        "ORDER BY deployed_at DESC LIMIT 1")
    if row is not None:
        return dict(row)

    identity = f"{settings.llm_backend}:{settings.gguf_path}:{settings.llm_n_ctx}"
    snapshot_id = "snap_gen_" + hashlib.sha256(identity.encode()).hexdigest()[:16]
    weight_hash = "sha256:" + hashlib.sha256(identity.encode()).hexdigest()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    db.run_write(lambda conn: conn.execute(
        "INSERT OR IGNORE INTO model_snapshots (snapshot_id, role, supply, "
        "inference_scope, provider, model_name, weight_hash, ctx_limit, "
        "deployed_at, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (snapshot_id, "generate", "self_hosted", "local_single_gpu",
         "local", settings.llm_backend, weight_hash, settings.llm_n_ctx,
         now, "active"),
    ))
    row = db.fetch_one("SELECT * FROM model_snapshots WHERE snapshot_id=?",
                       (snapshot_id,))
    if row is None:  # pragma: no cover - 只可能发生在写库失败
        raise AppError("MODEL_MISMATCH", "无法登记生成模型快照")
    return dict(row)


def measured_consistency(db: Database, snapshot_id: str) -> float | None:
    """读取实测判定一致率；没有记录返回 None。"""
    row = db.fetch_one(
        "SELECT measured_consistency FROM model_consistency WHERE snapshot_id=?",
        (snapshot_id,))
    return None if row is None else float(row["measured_consistency"])


def derive_eligibility(db: Database, snapshot: dict[str, Any]) -> str:
    """按快照与一致率派生 signable / draft_only。"""
    scope = str(snapshot.get("inference_scope") or "")
    if scope != "local_single_gpu":
        # 一期只允许本地单卡；集群场景不在 M2 放行范围
        return "draft_only"
    rate = measured_consistency(db, str(snapshot["snapshot_id"]))
    if rate is None:
        return "draft_only"
    return "signable" if rate >= CONSISTENCY_THRESHOLD else "draft_only"


def resolve_snapshot_for_request(
    db: Database, settings: Settings,
    claimed_inference_scope: str | None, actor: str,
) -> tuple[dict[str, Any], str | None]:
    """客户端若声明 inference_scope，记录忽略审计并返回告警码。"""
    snapshot = ensure_generate_snapshot(db, settings)
    warning = None
    if claimed_inference_scope:
        # AC-47D：模型能力与签署资格不得由客户端声明
        warning = "INFERENCE_SCOPE_IGNORED"
        get_logger("verify").warning(
            "inference_scope_ignored", actor=actor,
            claimed=claimed_inference_scope,
            actual=snapshot.get("inference_scope"))
        _write_audit(db, actor, "INFERENCE_SCOPE_IGNORED",
                     f"{actor}|claimed={claimed_inference_scope}")
    return snapshot, warning


def _write_audit(db: Database, actor: str, action: str, target: str) -> None:
    """追加审计（哈希链）；审计失败只记日志，不阻断主流程。"""
    created = _now()
    log_id = _id("aud")

    def _write(conn) -> None:
        row = conn.execute(
            "SELECT self_hash FROM audit_logs ORDER BY created_at DESC, log_id DESC LIMIT 1"
        ).fetchone()
        prev_hash = row["self_hash"] if row else None
        raw = f"{actor}|{action}|{target}|{prev_hash or ''}"
        self_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT INTO audit_logs (log_id, actor, action, target, prev_hash, "
            "self_hash, created_at) VALUES (?,?,?,?,?,?,?)",
            (log_id, actor, action, target, prev_hash, self_hash, created),
        )

    try:
        db.run_write(_write)
    except Exception as exc:  # pragma: no cover - 写库异常时不影响判定
        get_logger("verify").error("audit_write_failed", action=action,
                                   reason=str(exc)[:200])


def _id(prefix: str) -> str:
    """M2 最小可用 ID：前缀 + 时间戳 + 随机十六进制。"""
    import secrets

    return f"{prefix}_{int(time.time() * 1000):013d}{secrets.token_hex(6)}"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
