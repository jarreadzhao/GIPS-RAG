/* =====================================================================
   pages/docs.js · 文档管理
   展示已入库文档状态（后端 /documents 路由），上传表单暂占位。
   ===================================================================== */
import { $, $$, esc, setPagebar } from "../app.js";
import { t } from "../i18n.js";
import { getDocument } from "../api.js";

// 已知文档 ID（来自 gips.db documents 表）；英文侧是检索主体，排在前面
const KNOWN_DOCS = [
  { doc_id: "gips2020",    key: "docs.enName", roleKey: "docs.role.en" },
  { doc_id: "gips2020-zh", key: "docs.zhName", roleKey: "docs.role.zh" },
];

/* 把后端状态映射成徽章颜色 */
function statusBadge(value) {
  const v = String(value || "").toLowerCase();
  const cls = /(ready|indexed|done|succeeded|active|ok)/.test(v)
    ? "badge-ok"
    : /(fail|error)/.test(v) ? "badge-danger" : "badge-gray";
  return `<span class="badge ${cls}">${esc(value || "—")}</span>`;
}

async function refreshOne(row) {
  const statusCell = row.querySelector(".doc-status");
  const activeCell = row.querySelector(".doc-active");
  statusCell.innerHTML = `<span class="badge badge-gray">${t("loading")}</span>`;
  activeCell.innerHTML = `<span class="badge badge-gray">—</span>`;
  try {
    const data = await getDocument(row.dataset.id);
    row.querySelector(".doc-version").textContent = data.version_no ?? "?";
    row.querySelector(".doc-pages").textContent = data.page_count ?? "?";
    row.querySelector(".doc-text").textContent =
      data.text_layer_ratio != null ? (data.text_layer_ratio * 100).toFixed(1) + "%" : "?";
    statusCell.innerHTML = statusBadge(data.index_status || data.status);
    activeCell.innerHTML = data.active_version
      ? `<span class="badge badge-ok">${esc(data.active_version)}</span>`
      : `<span class="badge badge-gray">${t("docs.noActive")}</span>`;
  } catch (e) {
    statusCell.innerHTML = `<span class="badge badge-danger">${t("docs.failed")}${esc(e.message || "—")}</span>`;
  }
}

async function refreshAll() {
  const rows = $$(".doc-row");
  await Promise.all(rows.map(refreshOne));
}

export function renderDocs(container) {
  container.innerHTML = `
    <div class="page-title-row">
      <div>
        <h2 class="page-title">${t("docs.title")}</h2>
        <p class="page-desc">${t("docs.desc")}</p>
      </div>
      <div class="page-title-meta">
        <span class="badge badge-gray">${t("docs.meta")}</span>
      </div>
    </div>

    <div class="panel docs-panel">
      <div class="panel-title">${t("docs.title")}</div>
      <div class="panel-sub">${t("docs.sub")}</div>
      <div class="table-wrap">
        <table class="tbl">
          <thead>
            <tr>
              <th>${t("docs.th.doc")}</th>
              <th>${t("docs.th.docid")}</th>
              <th>${t("docs.th.role")}</th>
              <th>${t("docs.th.version")}</th>
              <th>${t("docs.th.pages")}</th>
              <th>${t("docs.th.text")}</th>
              <th>${t("docs.th.status")}</th>
              <th>${t("docs.th.active")}</th>
            </tr>
          </thead>
          <tbody>
            ${KNOWN_DOCS.map(d => `
              <tr class="doc-row" data-id="${esc(d.doc_id)}">
                <td>${esc(t(d.key))}</td>
                <td class="num">${esc(d.doc_id)}</td>
                <td>${esc(t(d.roleKey))}</td>
                <td class="num doc-version">—</td>
                <td class="num doc-pages">—</td>
                <td class="num doc-text">—</td>
                <td class="doc-status">—</td>
                <td class="doc-active">—</td>
              </tr>`).join("")}
          </tbody>
        </table>
      </div>
    </div>`;

  setPagebar({
    title: t("docs.title"),
    meta: t("docs.meta"),
    actions: `<button class="btn btn-ghost btn-sm" id="refreshAllBtn" type="button">${t("docs.refreshAll")}</button>`,
  });

  $("#refreshAllBtn").addEventListener("click", refreshAll);
  refreshAll();
}
