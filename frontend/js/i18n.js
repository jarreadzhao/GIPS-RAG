/* =====================================================================
   i18n.js · 界面语言（中文 / English）

   语言同时决定两件事：
     1. 界面文案（本文件的词典）
     2. 后端答案语种（作为 answer_lang 随 POST /reviews 下发）

   切换语言时整页重新加载：会话历史在 sessionStorage 里，不会丢；
   重载比逐处重渲染更不容易漏掉某块已经画到 DOM 上的旧文案。
   ===================================================================== */

const KEY = "gips_ui_lang";

const DICT = {
  zh: {
    /* ---------- 外壳 ---------- */
    "brand": "合规审查工作台",
    "brand.side": "标准查询工作台",
    "banner": "单机查询与自查工具 · 数据源：GIPS 2020 《Standards for Firms》 · 后端 <code>127.0.0.1:8011</code>",
    "backend.tag": "后端 :8011",
    "app.module": "GIPS 2020",
    "ctx.obj.all": "全部条款",
    "ctx.obj.docs": "文档清单",
    "ctx.chip": "Standards for Firms",
    "theme.toLight": "切换到浅色主题",
    "theme.toDark": "切换到深空主题",
    "nav.chat": "问答工作台",
    "nav.search": "条款检索",
    "nav.docs": "文档管理",
    "nav.eval": "评测与验收",
    "nav.disabled": "暂未开放",
    "health.online": "在线",
    "health.offline": "离线",
    "health.nodata": "在线（响应无数据）",
    "loading": "加载中…",
    "error.title": "页面加载失败",
    "toast.disabled": "本页面暂未开放",

    /* ---------- 问答工作台 ---------- */
    "chat.title": "条款问答",
    "chat.desc": "基于 GIPS 2020 原文的抽取式回答，每条结论都带可回链的引用片段",
    "chat.meta": "抽取式回答 · 引用可回链",
    "chat.history": "会话历史",
    "chat.emptyHistory": "还没有会话<br/>在右侧提问即可开始",
    "chat.new": "新会话",
    "chat.stream": "对话",
    "chat.count": "{n} 条",
    "chat.answer": "回答（Task {id}）",
    "chat.task": "Task {id}",
    "chat.placeholder": "问一条 GIPS 条款要求…  如：组合的定义是什么",
    "chat.send": "发送",
    "chat.sending": "回答中…",
    "chat.fail": "发送失败：",
    "chat.noAnswer": "（任务 {id} 状态 {st}，未返回答案）",
    "chat.langTitle": "回答语种",
    "chat.zh": "中文",
    "chat.en": "English",
    "chat.you": "你",
    "chat.refs": "引用片段（{n}）",
    "chat.noRefs": "本条回答没有可展示的引用片段",
    "chat.refMissing": "（片段缺失）",
    "chat.refLangZh": "中文",
    "chat.refLangEn": "EN",
    "chat.refHash": "hash {h}…",
    "chat.emptyTitle": "还没有对话",
    "chat.emptyDesc": "在下方输入一条 GIPS 条款问题，回答会附带可回链的原文片段",
    "chat.shortcut": "Ctrl / ⌘ + Enter 发送",
    "chat.verdict": "判定",

    /* ---------- 条款检索 ---------- */
    "search.title": "条款检索",
    "search.desc": "稠密 + 稀疏 + 结构化三通道召回，RRF 融合后返回条款与原始候选",
    "search.meta": "dense / sparse / sql",
    "search.placeholder": "输入条款编号 / 标题 / 义务原文关键词…  如：1.A.4、carve-out、record retention",
    "search.btn": "搜索",
    "search.searching": "搜索中…",
    "search.needQuery": "请输入查询词",
    "search.failed": "检索失败：",
    "search.intro": "在上方输入查询词开始检索；示例：",
    "search.emptyTitle": "还没有检索",
    "search.query": "查询",
    "search.channels": "检索通道",
    "search.top5": "Top-5 重排条款",
    "search.raw": "原始候选（{n} 条）",
    "search.noTop5": "无 Top-5 重排结果",
    "search.noCand": "无候选",
    "search.resultsMeta": "{p} 个条款 · {c} 条候选",
    "search.chunkScore": "score {s} · p{p}",
    "search.rank": "RRF {v}",
    "search.th.index": "#",
    "search.th.channel": "通道",
    "search.th.provision": "条款",
    "search.th.score": "score",
    "search.th.page": "page",
    "search.th.text": "text 前 260 字",
    "search.hit.sql": "结构化命中",
    "search.hit.dense": "稠密命中",
    "search.hit.sparse": "稀疏命中",
    "search.channelOff": "{c} 已关闭",

    /* ---------- 文档管理 ---------- */
    "docs.title": "文档管理",
    "docs.desc": "已入库文档的版本、解析与索引状态，数据实时读取后端",
    "docs.meta": "只读视图",
    "docs.sub": "点击「刷新全部」实时查询后端状态；入库操作走后端 API-9，暂不开放前端表单",
    "docs.refreshAll": "刷新全部",
    "docs.noActive": "无 active version",
    "docs.failed": "查询失败：",
    "docs.zhName": "GIPS 2020 《Standards for Firms》中文译版",
    "docs.enName": "GIPS 2020 《Standards for Firms》英文原文",
    "docs.th.doc": "文档",
    "docs.th.docid": "doc_id",
    "docs.th.role": "用途",
    "docs.th.version": "版本",
    "docs.th.pages": "页数",
    "docs.th.text": "文本层",
    "docs.th.status": "索引状态",
    "docs.th.active": "active",
    "docs.role.en": "检索主体",
    "docs.role.zh": "中文对照 · display_only",
  },
  en: {
    /* ---------- Shell ---------- */
    "brand": "Compliance Review Workbench",
    "brand.side": "Standards Console",
    "banner": "Standalone query & self-check tool · Data source: GIPS 2020 Standards for Firms · backend <code>127.0.0.1:8011</code>",
    "backend.tag": "Backend :8011",
    "app.module": "GIPS 2020",
    "ctx.obj.all": "All provisions",
    "ctx.obj.docs": "Document inventory",
    "ctx.chip": "Standards for Firms",
    "theme.toLight": "Switch to light theme",
    "theme.toDark": "Switch to dark theme",
    "nav.chat": "Q&A Workbench",
    "nav.search": "Provision Search",
    "nav.docs": "Documents",
    "nav.eval": "Evaluation",
    "nav.disabled": "Coming soon",
    "health.online": "Online",
    "health.offline": "Offline",
    "health.nodata": "Online (empty response)",
    "loading": "Loading…",
    "error.title": "Page failed to load",
    "toast.disabled": "This page is not available yet",

    /* ---------- Q&A workbench ---------- */
    "chat.title": "Provision Q&A",
    "chat.desc": "Extractive answers grounded in GIPS 2020 text; every statement links back to a verifiable snippet",
    "chat.meta": "Extractive · traceable citations",
    "chat.history": "Session history",
    "chat.emptyHistory": "No sessions yet<br/>Ask a question to start",
    "chat.new": "New session",
    "chat.stream": "Conversation",
    "chat.count": "{n} msg(s)",
    "chat.answer": "Answer (Task {id})",
    "chat.task": "Task {id}",
    "chat.placeholder": "Ask about a GIPS provision…  e.g. how is a composite defined",
    "chat.send": "Send",
    "chat.sending": "Answering…",
    "chat.fail": "Failed to send: ",
    "chat.noAnswer": "(Task {id} status {st}, no answer returned)",
    "chat.langTitle": "Answer language",
    "chat.zh": "中文",
    "chat.en": "English",
    "chat.you": "You",
    "chat.refs": "Citations ({n})",
    "chat.noRefs": "No citation is available for this answer",
    "chat.refMissing": "(snippet missing)",
    "chat.refLangZh": "中文",
    "chat.refLangEn": "EN",
    "chat.refHash": "hash {h}…",
    "chat.emptyTitle": "No conversation yet",
    "chat.emptyDesc": "Enter a GIPS provision question below; the answer will carry traceable source snippets",
    "chat.shortcut": "Ctrl / ⌘ + Enter to send",
    "chat.verdict": "Verdict",

    /* ---------- Provision search ---------- */
    "search.title": "Provision Search",
    "search.desc": "Dense + sparse + structured recall, fused by RRF, returning provisions and raw candidates",
    "search.meta": "dense / sparse / sql",
    "search.placeholder": "Provision number / title / obligation keywords…  e.g. 1.A.4, carve-out, record retention",
    "search.btn": "Search",
    "search.searching": "Searching…",
    "search.needQuery": "Please enter a query",
    "search.failed": "Search failed: ",
    "search.intro": "Enter a query above to start; example: ",
    "search.emptyTitle": "No search yet",
    "search.query": "Query",
    "search.channels": "Channels",
    "search.top5": "Top-5 reranked provisions",
    "search.raw": "Raw candidates ({n})",
    "search.noTop5": "No reranked Top-5 result",
    "search.noCand": "No candidates",
    "search.resultsMeta": "{p} provisions · {c} candidates",
    "search.chunkScore": "score {s} · p{p}",
    "search.rank": "RRF {v}",
    "search.th.index": "#",
    "search.th.channel": "channel",
    "search.th.provision": "provision",
    "search.th.score": "score",
    "search.th.page": "page",
    "search.th.text": "first 260 chars of text",
    "search.hit.sql": "structured hit",
    "search.hit.dense": "dense hit",
    "search.hit.sparse": "sparse hit",
    "search.channelOff": "{c} disabled",

    /* ---------- Documents ---------- */
    "docs.title": "Documents",
    "docs.desc": "Version, parsing and index status of ingested documents, read live from the backend",
    "docs.meta": "Read-only view",
    "docs.sub": "Click Refresh all to query backend status; ingestion (API-9) has no frontend form yet",
    "docs.refreshAll": "Refresh all",
    "docs.noActive": "no active version",
    "docs.failed": "Query failed: ",
    "docs.zhName": "GIPS 2020 Standards for Firms (Chinese translation)",
    "docs.enName": "GIPS 2020 Standards for Firms (English original)",
    "docs.th.doc": "Document",
    "docs.th.docid": "doc_id",
    "docs.th.role": "Role",
    "docs.th.version": "Version",
    "docs.th.pages": "Pages",
    "docs.th.text": "Text layer",
    "docs.th.status": "Index status",
    "docs.th.active": "active",
    "docs.role.en": "Retrieval corpus",
    "docs.role.zh": "Chinese reference · display_only",
  },
};

let lang = localStorage.getItem(KEY) === "en" ? "en" : "zh";

export function getLang() {
  return lang;
}

export function setLang(next) {
  const v = next === "en" ? "en" : "zh";
  if (v === lang) return;
  localStorage.setItem(KEY, v);
  location.reload();
}

/** 取文案；{name} 形式的占位符用 vars 替换。 */
export function t(key, vars) {
  const table = DICT[lang] || DICT.zh;
  let s = table[key] ?? DICT.zh[key] ?? key;
  if (vars) {
    Object.keys(vars).forEach(k => { s = s.split("{" + k + "}").join(vars[k]); });
  }
  return s;
}
