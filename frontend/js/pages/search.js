/* =====================================================================
   pages/search.js · 条款检索工作台
   GET /provisions/search?q=…，Top-5 重排条款卡片 + 原始候选明细。
   ===================================================================== */
import { $, $$, esc, toast, setPagebar } from "../app.js";
import { t } from "../i18n.js";
import { searchProvisions } from "../api.js";

const CHANNELS = ["dense", "sparse", "sql"];   // 三通道，与后端参数一致
let activeChannels = new Set(CHANNELS);        // 当前启用的通道
let lastResult = null;

/* ---------- 初始化交互 ---------- */
function render() {
  const input = $("#searchInput");
  input.focus();
  $("#searchBtn").addEventListener("click", doSearch);
  input.addEventListener("keydown", e => { if (e.key === "Enter") doSearch(); });

  // 通道开关：至少保留一个，避免空通道请求
  $$("#channelSeg button").forEach(btn => {
    btn.addEventListener("click", () => {
      const ch = btn.dataset.ch;
      if (activeChannels.has(ch)) {
        if (activeChannels.size === 1) return;
        activeChannels.delete(ch);
      } else {
        activeChannels.add(ch);
      }
      btn.classList.toggle("active", activeChannels.has(ch));
    });
  });

  // 空状态里的示例词
  $$("#resultArea [data-example]").forEach(el => {
    el.addEventListener("click", () => {
      input.value = el.dataset.example;
      doSearch();
    });
  });

  if (location.hash.includes("?")) {
    const params = new URLSearchParams(location.hash.split("?")[1] || "");
    const q = params.get("q");
    if (q) { input.value = q; doSearch(); }
  }
}

/* ---------- 发起检索 ---------- */
async function doSearch() {
  const q = $("#searchInput").value.trim();
  if (!q) return toast(t("search.needQuery"), "warn");
  const btn = $("#searchBtn");
  btn.disabled = true; btn.textContent = t("search.searching");

  try {
    const resp = await searchProvisions({
      q,
      top_k: 20,
      channels: CHANNELS.filter(ch => activeChannels.has(ch)), // 按当前开关下发
    });
    lastResult = resp;
    drawResult(resp, q);
  } catch (e) {
    toast(t("search.failed") + (e.message || e), "error");
  } finally {
    btn.disabled = false; btn.textContent = t("search.btn");
  }
}

/* ---------- 渲染结果 ---------- */
function drawResult(resp, q) {
  const { top5, candidates, channels } = resp;
  const container = $("#resultArea");

  const provCards = (top5 || []).map(prov => {
    const chunks = (prov.chunks || []).map(c => `
      <div class="prov-chunk">
        <span class="badge badge-blue">${esc(c.channel || "—")}</span>
        <span class="mono">${t("search.chunkScore", {
          s: (c.score ?? 0).toFixed(3),
          p: c.page_no ?? "?",
        })}</span>
        <span class="chunk-text">${esc((c.text || "").slice(0, 160))}${(c.text || "").length > 160 ? "…" : ""}</span>
      </div>`).join("");

    // 命中的通道标签：从原始候选中反查
    const hitTags = (() => {
      if (!candidates) return "";
      const all = candidates.filter(c => c.provision_id === prov.provision_id);
      const hit = new Set(all.map(c => c.channel));
      const tags = [];
      if (hit.has("sql")) tags.push(`<span class="badge badge-ok">${t("search.hit.sql")}</span>`);
      if (hit.has("dense")) tags.push(`<span class="badge badge-en">${t("search.hit.dense")}</span>`);
      if (hit.has("sparse")) tags.push(`<span class="badge badge-gray">${t("search.hit.sparse")}</span>`);
      return tags.join(" ");
    })();

    return `<article class="prov-card">
      <div class="prov-head">
        <span class="prov-id">${esc(prov.provision_id)}</span>
        ${prov.type ? `<span class="badge badge-gray">${esc(prov.type)}</span>` : ""}
        ${hitTags}
        <span class="prov-rrf">${t("search.rank", { v: (prov.rrf_score ?? 0).toFixed(3) })}</span>
      </div>
      <div class="prov-text">${esc(prov.text || "")}</div>
      <div class="prov-chunks">${chunks}</div>
    </article>`;
  }).join("") || `<div class="empty-state"><div class="empty-title">${t("search.noTop5")}</div></div>`;

  const candRows = (candidates || []).map((c, i) => `
    <tr>
      <td class="num">${i + 1}</td>
      <td><span class="badge badge-gray">${esc(c.channel || "—")}</span></td>
      <td class="num">${esc((c.provision_id || "—").slice(0, 18))}</td>
      <td class="num">${(c.score ?? 0).toFixed(4)}</td>
      <td class="num">p${c.page_no ?? "?"}</td>
      <td class="wrap">${esc((c.text || "").slice(0, 260))}</td>
    </tr>`).join("") || `<tr><td colspan="6" class="empty-cell">${t("search.noCand")}</td></tr>`;

  const chStatus = Object.entries(channels || {}).map(([k, v]) => {
    if (v === "disabled") return `<span class="badge badge-gray">${t("search.channelOff", { c: k })}</span>`;
    return `<span class="badge badge-ok">${k} ✓</span>`;
  }).join(" ");

  // 页内工具条右侧同步结果计数
  const barMeta = $("#searchMeta");
  if (barMeta) barMeta.textContent = t("search.resultsMeta", {
    p: (top5 || []).length,
    c: (candidates || []).length,
  });

  container.innerHTML = `
    <div class="summary-bar">
      <span>${t("search.query")}: <b>${esc(q)}</b></span>
      <span>${t("search.resultsMeta", { p: (top5 || []).length, c: (candidates || []).length })}</span>
      <span>${chStatus}</span>
    </div>

    <div class="panel result-panel">
      <div class="panel-title">${t("search.top5")}<span class="spacer"></span><code>${esc(q)}</code></div>
      <div class="prov-list">${provCards}</div>
    </div>

    <div class="panel result-panel" style="margin-top:12px;">
      <div class="panel-title">${t("search.raw", { n: (candidates || []).length })}</div>
      <div class="table-wrap">
        <table class="tbl">
          <thead><tr>
            <th>${t("search.th.index")}</th>
            <th>${t("search.th.channel")}</th>
            <th>${t("search.th.provision")}</th>
            <th>${t("search.th.score")}</th>
            <th>${t("search.th.page")}</th>
            <th>${t("search.th.text")}</th>
          </tr></thead>
          <tbody>${candRows}</tbody>
        </table>
      </div>
    </div>`;
}

/* ---------- 页面 ---------- */
export function renderSearch(container) {
  container.innerHTML = `
    <div class="page-title-row">
      <div>
        <h2 class="page-title">${t("search.title")}</h2>
        <p class="page-desc">${t("search.desc")}</p>
      </div>
      <div class="page-title-meta">
        <span class="badge badge-gray">${t("search.meta")}</span>
      </div>
    </div>

    <div class="panel search-panel">
      <div class="toolbar">
        <input id="searchInput" class="input search-input" type="text" placeholder="${t("search.placeholder")}" />
        <span class="toolbar-label">${t("search.channels")}</span>
        <div class="seg" id="channelSeg">
          ${CHANNELS.map(ch => `<button type="button" data-ch="${ch}" class="active">${ch}</button>`).join("")}
        </div>
        <button id="searchBtn" class="btn btn-primary" type="button">${t("search.btn")}</button>
      </div>
    </div>

    <div id="resultArea" style="margin-top:12px;">
      <div class="panel">
        <div class="empty-state">
          <div class="empty-title">${t("search.emptyTitle")}</div>
          <div class="empty-desc">
            ${t("search.intro")}
            <button class="link-btn" type="button" data-example="portfolio company">portfolio company</button>
          </div>
        </div>
      </div>
    </div>`;

  setPagebar({
    title: t("search.title"),
    meta: t("search.meta"),
    actions: `<span class="panel-meta" id="searchMeta"></span>`,
  });

  render();
}
