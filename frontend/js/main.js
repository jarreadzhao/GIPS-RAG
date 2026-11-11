/* =====================================================================
   main.js · 入口：组装路由表并启动应用
   ===================================================================== */
import { startApp } from "./app.js";
import { t } from "./i18n.js";
import { renderChat } from "./pages/chat.js";
import { renderSearch } from "./pages/search.js";
import { renderDocs } from "./pages/docs.js";

const NAV = [
  { hash: "#/chat",   name: t("nav.chat"),   icon: svg('<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>') },
  { hash: "#/search", name: t("nav.search"), icon: svg('<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>') },
  { hash: "#/docs",   name: t("nav.docs"),   icon: svg('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>') },
  { hash: "#/eval",   name: t("nav.eval"),   icon: svg('<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>'), disabled: true },
];

function svg(d) {
  return `<svg class="nav-ico" viewBox="0 0 24 24" width="16" height="16" fill="none"
    stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
}

startApp({
  nav: NAV,
  pages: {
    "/chat":   { name: t("nav.chat"),   render: renderChat },
    "/search": { name: t("nav.search"), render: renderSearch },
    "/docs":   { name: t("nav.docs"),   render: renderDocs },
  },
});
