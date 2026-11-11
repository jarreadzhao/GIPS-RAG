/* =====================================================================
   app.js · 应用基座（hash 路由、导航、页内工具条、上下文条、主题、Toast）
   结构参考 finance_project，去掉本项目用不上的命令面板/登录等装饰。
   ===================================================================== */
import { healthz } from "./api.js";
import { t } from "./i18n.js";

// ---------- 通用 DOM 工具 ----------
export const $ = (sel, root) => (root || document).querySelector(sel);
export const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
export const esc = s => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
export const sleep = ms => new Promise(r => setTimeout(r, ms));

// ---------- Toast ----------
export function toast(msg, type) {
  const root = $("#toastRoot");
  if (!root) return;
  const el = document.createElement("div");
  el.className = "toast" + (type ? " toast-" + type : "");
  el.textContent = msg;
  root.appendChild(el);
  setTimeout(() => el.remove(), 2800);
}

// ---------- 主题（深空默认 / 浅色可选） ----------
const THEME_KEY = "gips_ui_theme";
// 深空主题下按钮展示太阳图标：点它去浅色
const ICON_SUN = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor"
  stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="4"></circle>
  <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"></path></svg>`;
// 浅色主题下按钮展示月亮图标：点它回深空
const ICON_MOON = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor"
  stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"></path></svg>`;

function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}

function syncThemeBtn() {
  const btn = $("#themeToggle");
  if (!btn) return;
  const dark = currentTheme() === "dark";
  btn.innerHTML = dark ? ICON_SUN : ICON_MOON;                // 图标表示“点了会变成什么”
  const label = dark ? t("theme.toLight") : t("theme.toDark");
  btn.title = label;
  btn.setAttribute("aria-label", label);
}

export function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* 隐私模式忽略 */ }
  syncThemeBtn();
}

// ---------- 斜杠分隔的路径渲染 ----------
function renderPath(segs) {
  return (segs || []).filter(Boolean)
    .map(s => `<span class="ctx-seg">${esc(s)}</span>`)
    .join('<span class="ctx-sep">/</span>');
}

// ---------- 导航 ----------
let NAV = [];

export function navigate(hash) {
  if (!hash.startsWith("#")) hash = "#" + hash;
  location.hash = hash;
}

function renderShell() {
  $("#navList").innerHTML = NAV.map(n => `
    <div class="nav-item ${n.disabled ? "disabled" : ""}" data-hash="${n.hash}"
         aria-label="${n.name}" title="${n.disabled ? t("nav.disabled") : n.name}">
      ${n.icon}
      <span class="nav-label">${n.name}</span>
      ${n.disabled ? `<span class="nav-tag">${t("nav.disabled")}</span>` : ""}
      <span class="nav-tip">${n.name}</span>
    </div>`).join("");
  $$("#navList .nav-item").forEach(el => {
    el.addEventListener("click", () => {
      if (el.classList.contains("disabled")) { toast(t("toast.disabled")); return; }
      navigate(el.dataset.hash);
    });
  });
}

function renderActiveNav(hash) {
  $$("#navList .nav-item").forEach(el => {
    el.classList.toggle("active", el.dataset.hash === hash);
  });
}

// ---------- 路由 ----------
function parseHash() {
  const raw = (location.hash || "#/chat").replace(/^#/, "");
  const [path, query] = raw.split("?");
  const params = new URLSearchParams(query || "");
  return { path: path || "/chat", params };
}

let _pages = {};  // { path: { name, render } }
let _currentPath = null;

/** 设置页内工具条：接受对象 { title, meta, actions } 或直接一段 HTML。 */
export function setPagebar(arg) {
  const el = $("#pagebar");
  if (!el) return null;
  if (typeof arg === "string") { el.innerHTML = arg; return el; }
  const { title = "", meta = "", actions = "" } = arg || {};
  el.innerHTML = `
    <div class="pagebar-left">
      ${title ? `<span class="pagebar-title">${esc(title)}</span>` : ""}
      ${meta ? `<span class="pagebar-meta">${esc(meta)}</span>` : ""}
    </div>
    <div class="pagebar-right">${actions}</div>`;
  return el;
}

/** 面包屑：接受字符串或数组；最后一段加粗。 */
export function setBreadcrumb(crumbs) {
  const el = $("#breadcrumb");
  if (!el) return;
  const list = (Array.isArray(crumbs) ? crumbs : [crumbs]).filter(Boolean);
  el.innerHTML = list.map((c, i) => i === list.length - 1
    ? `<b class="crumb">${esc(c)}</b>`
    : `<span class="crumb">${esc(c)}</span>`
  ).join('<span class="crumb-sep" aria-hidden="true">/</span>');
}

/** 顶栏上下文条：模块 / 页面 / 当前对象 + 右侧 chip。 */
export function setContext({ segs = [], chip = "" } = {}) {
  const path = $("#ctxPath");
  if (path) path.innerHTML = renderPath(segs.slice(0, 3));
  const chipEl = $("#ctxChip");
  if (chipEl && chip) chipEl.textContent = chip;
}

function contextFor(path) {
  const page = _pages[path] || _pages["/chat"];
  const name = page ? page.name : t("brand");
  const obj = path === "/docs" ? t("ctx.obj.docs") : t("ctx.obj.all");
  return { segs: [t("app.module"), name, obj], chip: t("ctx.chip") };
}

const pageTitle = page => t("brand") + " · " + (page ? page.name : "");

async function onRoute() {
  const { path, params } = parseHash();
  renderActiveNav("#" + path);
  const page = _pages[path] || _pages["/chat"];
  if (!page) return;
  setBreadcrumb([t("brand"), page.name]);
  setContext(contextFor(path));
  setPagebar({ title: page.name });           // 页面可在 render 里覆盖
  document.title = pageTitle(page);
  const container = $("#pageContainer");
  container.innerHTML = `<div class="loading">${t("loading")}</div>`;
  _currentPath = path;
  try {
    await page.render(container, params);
  } catch (e) {
    if (_currentPath !== path) return;        // 已切路由就别渲染了
    container.innerHTML = `
      <div class="error-card">
        <div class="error-title">${t("error.title")}</div>
        <pre class="error-msg">${esc(e.message || e)}</pre>
      </div>`;
  }
}

// ---------- 后端心跳 ----------
async function pollBackendHealth() {
  const dot = $("#backendHealth");
  if (!dot) return;
  let online = false;
  try {
    const data = await healthz();
    online = !!data;
    dot.innerHTML = online
      ? `<i class="dot dot-live"></i>${t("health.online")} v${esc(data.version || "")}`
      : `<i class="dot dot-idle"></i>${t("health.nodata")}`;
  } catch (e) {
    dot.innerHTML = `<i class="dot dot-dead"></i>${t("health.offline")}`;
  }
  dot.classList.toggle("is-dead", !online);
}

// ---------- 顶栏时钟 ----------
function tickClock() {
  const el = $("#clock");
  if (!el) return;
  const d = new Date();
  const p = n => String(n).padStart(2, "0");
  el.textContent = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// ---------- 启动 ----------
export function startApp({ nav, pages }) {
  NAV = nav;
  _pages = pages;

  const brand = $("#brandTitle");
  if (brand) brand.textContent = t("brand.side");   // 侧栏空间有限，用短名
  const brandBox = $("#brandBox");
  if (brandBox) brandBox.title = t("brand");         // 完整名称放 title
  const banner = $("#demoBanner");
  if (banner) banner.innerHTML = t("banner");
  const tag = $("#backendTag");
  if (tag) tag.textContent = t("backend.tag");
  const ctxChip = $("#ctxChip");
  if (ctxChip) ctxChip.textContent = t("ctx.chip");

  const themeBtn = $("#themeToggle");
  if (themeBtn) themeBtn.addEventListener("click", toggleTheme);
  syncThemeBtn();

  renderShell();
  window.addEventListener("hashchange", onRoute);
  onRoute();

  pollBackendHealth();
  setInterval(pollBackendHealth, 30000);

  tickClock();
  setInterval(tickClock, 1000);
}
