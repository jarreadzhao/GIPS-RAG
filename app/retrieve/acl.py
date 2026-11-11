"""ACL 解析与前置过滤表达式构造（R-8、R-18、R-25）。

边界（契约 2.3）：
    - `acl_scope` 由网关注入，检索侧不得接受客户端传入的值；
    - 缺失即拒绝（400 ACL_SCOPE_MISSING），不得「无过滤即查全库」；
    - 过滤必须作为 Milvus search 的 filter 前置条件，不能拿到结果后再筛。

本模块同时提供 `role_view` 到 `acl_scope` 的映射，供召回测试台（API-17）模拟角色。
映射表在规格中只写了「角色与密级映射表已确认」，未给具体数值，
故在代码里显式落一份最小权限默认值，并在 M1 报告中登记为待确认项（A-1）。
"""
from __future__ import annotations

import json

from app.errors import AppError

# 角色 -> 可见密级。默认最小权限：observer / reviewer 只能看 internal。
ROLE_CLEARANCE: dict[str, tuple[str, ...]] = {
    "observer": ("internal",),
    "reviewer": ("internal",),
    "librarian": ("internal", "restricted"),
    "approver": ("internal", "restricted"),
    "admin": ("internal", "restricted"),
}

# 允许使用 role_view 模拟他人视角的角色（接口 API-17）
ROLE_VIEW_ALLOWED = ("admin", "approver")

ALL_CLEARANCES = ("internal", "restricted")
TRUSTED_GATEWAY_HEADER = "X-Gateway-Trusted"


def parse_scope(raw: str | list[str] | None) -> list[str] | None:
    """解析 acl_scope。返回 None 表示缺失（调用方必须拒绝）。

    空列表是合法值，但与缺失不同义：空列表只匹配公开件。
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    text = raw.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        # 逗号分隔的简化形态也接受，但必须是已知标签
        value = [p.strip() for p in text.split(",") if p.strip()]
    if not isinstance(value, list):
        return None
    return [str(x).strip() for x in value]


def split_scope(scope: list[str]) -> tuple[list[str], list[str]]:
    """把 ['dept:FUND','clr:internal'] 拆成 (depts, clearances)。

    未知前缀一律忽略；它们不构成授权，不能默认放行。
    """
    depts: list[str] = []
    clearances: list[str] = []
    for item in scope:
        if item.startswith("dept:") and len(item) > 5:
            depts.append(item[5:])
        elif item.startswith("clr:") and len(item) > 4:
            clearances.append(item[4:])
    return depts, clearances


def scope_from_role(role: str, dept: str) -> list[str]:
    """按角色与部门生成 acl_scope（网关注入逻辑的本地替身）。"""
    if role not in ROLE_CLEARANCE:
        raise AppError("ACL_DENIED", f"未知角色 {role}，拒绝检索")
    if not dept:
        raise AppError("ACL_SCOPE_MISSING", "网关注入的身份缺少 dept，无法生成 acl_scope")
    return [f"dept:{dept}"] + [f"clr:{c}" for c in ROLE_CLEARANCE[role]]


def resolve_acl_scope(request) -> list[str] | None:
    """从网关请求头解析 acl_scope。

    只认两种来源：
        1. 可信网关头 `X-Gateway-Scope`（需同时带 `X-Gateway-Trusted: 1`）；
        2. `X-Role` + `X-Dept` 由本服务按角色映射生成。

    查询参数或请求体里的 `acl_scope` 一律忽略（AC-25：客户端伪造无效）。
    """
    trusted = request.headers.get(TRUSTED_GATEWAY_HEADER, "")
    gateway_scope = request.headers.get("X-Gateway-Scope")
    if trusted in ("1", "true", "True") and gateway_scope:
        return parse_scope(gateway_scope)

    role = request.headers.get("X-Role", "").strip()
    dept = request.headers.get("X-Dept", "").strip()
    if role and dept:
        return scope_from_role(role, dept)
    return None


def actor_role(request) -> str:
    """取调用者角色；仅用于 API-17 的 role_view 权限判断。"""
    return request.headers.get("X-Role", "").strip()


def assert_role_view_allowed(role: str, role_view: str | None) -> None:
    """role_view 只允许 admin 与 approver 使用（接口 API-17）。"""
    if role_view and role not in ROLE_VIEW_ALLOWED:
        raise AppError("ACL_DENIED", f"角色 {role or '(未提供)'} 无权使用 role_view 模拟他人视角")
    if role_view and role_view not in ROLE_CLEARANCE:
        raise AppError("ACL_DENIED", f"未知的 role_view：{role_view}")
