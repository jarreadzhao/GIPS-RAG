# ACL 反例测试报告

- 阶段：M1 检索可度量（AC-15、AC-16、AC-25、AC-26）
- 生成日期：2026-11-09
- 服务版本：`0.1.0-M1`，主实例 `127.0.0.1:8011`，索引 `idx_58bd974484e82424`
- 规则：`acl_scope` 只接受网关注入（`X-Gateway-Trusted: 1` + `X-Gateway-Scope`，或 `X-Role` + `X-Dept`）；客户端传入一律忽略

---

## 1. 四类反例总表

| 类别 | 用例 | 期望 | 实测 | 结论 |
|---|---|---|---|---|
| 缺失参数 | 不带任何网关身份头检索 | 400 `ACL_SCOPE_MISSING`，且不查库 | 400 `ACL_SCOPE_MISSING` | 通过 |
| 越权角色 | `observer` 检索其他部门 | 200 空集，`empty_reason=acl_filtered` | 0 条，`acl_filtered` | 通过 |
| 越权角色 | `observer` 使用 `role_view=admin` | 403 `ACL_DENIED` | 403 `ACL_DENIED` | 通过 |
| 跨密级 | `observer` 检索 `restricted` 文档 | 0 条，`acl_filtered` | 0 条，`acl_filtered` | 通过 |
| 跨密级 | `librarian` 检索同部门 `restricted` 文档 | 可见 | 2 条，Top `1.A.9` | 通过 |
| 伪造参数 | query string 塞 `acl_scope=["dept:risk","clr:restricted"]` | 忽略伪造值，按身份过滤 | 仍返回本部门 5 条 | 通过 |
| 伪造参数 | 请求体塞 `acl_scope`（API-17） | 网关身份覆盖 | `acl_scope_used=["dept:compliance","clr:internal"]`，5 条 | 通过 |
| 台账隔离 | 检索连接访问 `calc_results` | 连接/鉴权失败 | SQLite `DatabaseError`，0 行 | 通过 |

---

## 2. 现场证据（主实例 8011）

固定查询 `record keeping requirements`，`X-Dept: compliance`：

| 请求 | 状态 | 结果 |
|---|---|---|
| `GET /provisions/search`，无身份头 | 400 | `code=ACL_SCOPE_MISSING` |
| `GET /provisions/search`，`X-Role: observer`，`X-Dept: risk` | 200 | `reranked=0`，`empty_reason=acl_filtered` |
| `GET /provisions/search`，`X-Role: observer`，`X-Dept: compliance` | 200 | `reranked=5`，`empty_reason=""` |
| `GET /provisions/search?...&acl_scope=["dept:risk","clr:restricted"]`，`observer/compliance` | 200 | `reranked=5`（伪造值未生效，且日志记 `acl_scope_forged_ignored`） |
| `POST /retrieval/test`，body 含伪造 `acl_scope`，`observer/compliance` | 200 | `acl_scope_used=["dept:compliance","clr:internal"]`，`hits=5` |
| `POST /retrieval/test`，`role_view=admin`，`X-Role: observer` | 403 | `code=ACL_DENIED` |

说明：第 4 条中返回 5 条而非空集，正是「伪造被忽略」的正确表现——服务按 `observer/compliance` 的真实权限返回；若伪造生效，`dept:risk` 会筛空。

## 3. 跨密级隔离实验（独立实例）

主语料只有 `internal` 密级，无法验证跨密级，故在隔离环境单独验证（独立 SQLite 库、独立 Milvus collection、独立端口 8012），上传一份 `dept=risk`、`clearance=restricted` 的 2-chunk PDF：

| 身份 | 结果 |
|---|---|
| `observer` / `risk` | 0 条，`empty_reason=acl_filtered` |
| `librarian` / `risk` | 2 条，Top `1.A.9` |

实验完成后已删除临时 collection、SQLite 库、PDF 与日志，不影响主实例。跨密级判定依据是 `acl_scope` 中的 `clr:` 标签进入 Milvus 前置过滤，`observer` 只映射到 `clr:internal`。

## 4. 台账 schema 隔离（AC-26）

- 检索通道使用 SQLite 只读连接（`mode=ro`）+ authorizer，只放行白名单视图的 `SELECT`。
- 单测 `tests/test_retrieval.py::test_ro_connection_denies_ledger_table`：直接查询 `calc_results` 抛 `sqlite3.DatabaseError`。
- 单测 `tests/test_retrieval.py::test_calc_results_attempt_is_audited`：经结构化通道路径尝试时，`SQL_GUARD_REJECTED` 且写 `audit_logs`。

## 5. 单测覆盖

| AC | 用例 |
|---|---|
| AC-15 | `test_observer_cannot_see_other_dept_acl_filtered`、`test_role_clearance_mapping_blocks_cross_clearance` |
| AC-16 | `test_acl_missing_rejected_before_any_query`、`test_api_missing_scope_returns_400` |
| AC-25 | `test_api_ignores_forged_acl_scope_in_query_string`、`test_api_17_forged_acl_scope_in_body_is_ignored` |
| AC-26 | `test_ro_connection_denies_ledger_table`、`test_calc_results_attempt_is_audited` |

全部 74 项测试通过（`python -m pytest -q`）。

## 6. 遗留

- **CONF-M1-01**：角色→密级映射表在规格中未给出具体数值，当前代码为最小权限默认值（`observer/reviewer=internal`；`librarian/approver/admin=internal+restricted`），需合规确认后回填。
- ACL 反例 100% 拦截，可作为 M1 放行依据之一；但 M1 整体仍被 golden 缺失阻塞（见 [`检索基线-v1.md`](./检索基线-v1.md)）。
