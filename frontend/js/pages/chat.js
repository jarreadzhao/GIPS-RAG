/* =====================================================================
   pages/chat.js · 问答工作台
   会话历史（sessionStorage）+ POST /reviews + GET /reviews/{task_id}
   引用卡片按 citations[].lang 展示单一语种；answer_lang 由界面语言决定。
   ===================================================================== */
import { $, $$, esc, toast, sleep, setPagebar } from "../app.js";
import { getLang, setLang, t } from "../i18n.js";
import { askQA, getReview } from "../api.js";

const HIST_KEY = "gips_chat_history_v1";
// 终态集合：只有这两个状态表示任务不再变化，其余一律继续轮询
const TERMINAL = new Set(["succeeded", "failed"]);

function loadHistory() {
  try { return JSON.parse(sessionStorage.getItem(HIST_KEY) || "[]"); }
  catch { return []; }
}
function saveHistory(list) {
  sessionStorage.setItem(HIST_KEY, JSON.stringify(list.slice(-30)));
}

let state = {
  sessions: loadHistory(),
  activeSessionId: null,
};

function makeSession(title) {
  return {
    id: "s_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
    title: title || t("chat.new"),
    ts: Date.now(),
    messages: [],
  };
}

function activeSession() {
  return state.sessions.find(s => s.id === state.activeSessionId);
}

/* ---------- 左栏：会话历史 ---------- */
function renderHistory() {
  const root = $("#historyList");
  if (!state.sessions.length) {
    root.innerHTML = `<div class="chat-history-empty">${t("chat.emptyHistory")}</div>`;
    return;
  }
  root.innerHTML = state.sessions.map(s => `
    <div class="chat-history-item ${s.id === state.activeSessionId ? "active" : ""}" data-id="${s.id}"
         role="button" tabindex="0">
      <div class="history-title">${esc(s.title)}</div>
      <div class="history-meta">${new Date(s.ts).toLocaleString()} · ${t("chat.count", { n: s.messages.length })}</div>
    </div>`).join("");
  $$("#historyList .chat-history-item").forEach(el => {
    el.addEventListener("click", () => {
      state.activeSessionId = el.dataset.id;
      saveHistory(state.sessions);
      renderAll();
    });
  });
}

/* ---------- 引用卡片 ---------- */
function renderCitation(c, index) {
  // 新记录按 answer_lang 只带一种语种；旧记录仍可能带 translations
  const zh = c.lang === "zh";
  const legacy = (c.translations || [])
    .map(x => `<div class="ref-snippet ref-zh">${esc(x.span_text)}</div>`).join("");
  const range = `char [${c.char_start ?? "?"}, ${c.char_end ?? "?"})`;
  return `<div class="ref-card">
    <div class="ref-head">
      <span class="ref-index">#${index + 1}</span>
      <span class="badge badge-blue">${esc(c.provision_id || "—")}</span>
      <span class="badge badge-gray">p${c.page_no ?? "?"}</span>
      <span class="badge ${zh ? "badge-zh" : "badge-en"}">${zh ? t("chat.refLangZh") : t("chat.refLangEn")}</span>
      <span class="ref-range mono">${esc(range)}</span>
    </div>
    <div class="ref-snippet ${zh ? "ref-zh" : "ref-en"}">${esc(c.span_text || t("chat.refMissing"))}</div>
    ${legacy}
    <div class="ref-meta">${t("chat.refHash", { h: esc((c.span_hash || "—").slice(0, 16)) })}</div>
  </div>`;
}

/* ---------- 右侧：消息流 ---------- */
function renderMessage(msg, index) {
  if (msg.role === "user") {
    return `<div class="msg-row msg-row-user" data-idx="${index}">
      <div class="msg-user">
        <span class="badge badge-gray">${t("chat.you")}</span>
        <p>${esc(msg.text)}</p>
      </div>
    </div>`;
  }

  const citations = Array.isArray(msg.citations) ? msg.citations : [];
  const refs = citations.length
    ? `<div class="ref-list">
         <div class="ref-list-head">${t("chat.refs", { n: citations.length })}</div>
         ${citations.map(renderCitation).join("")}
       </div>`
    : `<div class="ref-empty">${t("chat.noRefs")}</div>`;
  const verdict = msg.verdict
    ? `<span class="badge badge-warn">${t("chat.verdict")}: ${esc(msg.verdict)}</span>` : "";

  return `<div class="msg-row msg-row-ai" data-idx="${index}">
    <div class="msg-ai">
      <div class="msg-ai-head">
        <span class="badge badge-ai">AI</span>
        <span class="mono">${t("chat.task", { id: esc((msg.task_id || "").slice(0, 14)) })}</span>
        ${verdict}
      </div>
      <div class="msg-text">${esc(msg.text)}</div>
      ${refs}
    </div>
  </div>`;
}

function renderMessages() {
  const sess = activeSession();
  const root = $("#messageList");
  const meta = $("#streamMeta");
  if (!sess || !sess.messages.length) {
    root.innerHTML = `<div class="empty-state">
      <div class="empty-title">${t("chat.emptyTitle")}</div>
      <div class="empty-desc">${t("chat.emptyDesc")}</div>
    </div>`;
    if (meta) meta.textContent = "";
    $("#input").value = "";
    return;
  }
  root.innerHTML = sess.messages.map(renderMessage).join("");
  if (meta) meta.textContent = t("chat.count", { n: sess.messages.length });
  root.scrollTop = root.scrollHeight;
  $("#input").value = "";
}

function renderAll() {
  renderHistory();
  renderMessages();
}

function newSession() {
  const sess = makeSession();
  state.sessions.unshift(sess);
  state.activeSessionId = sess.id;
  saveHistory(state.sessions);
  renderAll();
  $("#input").focus();
}

/* ---------- 发送与轮询 ---------- */
async function handleSend() {
  const ta = $("#input");
  const btn = $("#sendBtn");
  const text = ta.value.trim();
  if (!text) return;

  let sess = activeSession();
  if (!sess) {
    sess = makeSession(text.slice(0, 20));
    state.sessions.unshift(sess);
    state.activeSessionId = sess.id;
  }
  if (sess.messages.length === 0) { sess.title = text.slice(0, 20); sess.ts = Date.now(); }
  sess.messages.push({ role: "user", text });
  saveHistory(state.sessions);
  renderAll();

  btn.disabled = true;
  btn.textContent = t("chat.sending");
  try {
    const history = sess.messages.filter(m => m.role !== "assistant").map(m => ({ role: m.role, content: m.text }));
    // POST /reviews 对 qa 任务同步返回终态（status/answer/citations 都在顶层），
    // 只有非终态时才回落到 GET /reviews/{task_id}——那里的状态在 task.status 下。
    let data = await askQA({ text, history, lang: getLang() });
    const tid = data.task_id;
    if (!data.status || !TERMINAL.has(data.status)) {
      for (let i = 0; i < 20; i++) {
        await sleep(400);
        const d = await getReview(tid);
        const st = (d && d.task && d.task.status) || (d && d.status);
        data = { ...(d || {}), status: st };
        if (st && TERMINAL.has(st)) break;
      }
    }

    const answerText = (data.answer || "").trim()
      || t("chat.noAnswer", { id: tid, st: data.status || "?" });
    const citations = Array.isArray(data.citations) ? data.citations : [];
    const verdict = data.findings && data.findings[0] && data.findings[0].verdict;

    sess.messages.push({ role: "assistant", text: answerText, task_id: tid, citations, verdict });
    saveHistory(state.sessions);
    renderAll();
  } catch (e) {
    toast(t("chat.fail") + (e.message || e), "error");
  } finally {
    btn.disabled = false;
    btn.textContent = t("chat.send");
  }
}

/* ---------- 页面 ---------- */
export function renderChat(container) {
  const lang = getLang();
  container.innerHTML = `
    <div class="page-title-row">
      <div>
        <h2 class="page-title">${t("chat.title")}</h2>
        <p class="page-desc">${t("chat.desc")}</p>
      </div>
      <div class="page-title-meta">
        <span class="badge badge-gray">${t("chat.meta")}</span>
      </div>
    </div>

    <div class="chat-layout">
      <aside class="panel chat-history">
        <div class="panel-title">
          ${t("chat.history")}
          <span class="spacer"></span>
          <button class="btn btn-sm btn-ghost" id="newBtn" type="button">${t("chat.new")}</button>
        </div>
        <div id="historyList" class="history-list"></div>
      </aside>

      <section class="chat-main">
        <div class="panel chat-stream-panel">
          <div class="panel-title">
            ${t("chat.stream")}
            <span class="spacer"></span>
            <span class="panel-meta" id="streamMeta"></span>
          </div>
          <div class="chat-messages" id="messageList"></div>
        </div>

        <div class="panel chat-composer">
          <textarea id="input" placeholder="${t("chat.placeholder")}"></textarea>
          <div class="composer-actions">
            <span class="form-hint">${t("chat.shortcut")}</span>
            <button class="btn btn-primary" id="sendBtn" type="button">${t("chat.send")}</button>
          </div>
        </div>
      </section>
    </div>`;

  // 语种切换放到页内工具条，和参考项目一致
  setPagebar({
    title: t("chat.title"),
    meta: t("chat.meta"),
    actions: `
      <span class="pagebar-meta">${t("chat.langTitle")}</span>
      <div class="seg" id="langSeg">
        <button type="button" class="${lang === "zh" ? "active" : ""}" data-lang="zh">${t("chat.zh")}</button>
        <button type="button" class="${lang === "en" ? "active" : ""}" data-lang="en">${t("chat.en")}</button>
      </div>`,
  });
  $$("#pagebar .seg button").forEach(el => {
    el.addEventListener("click", () => setLang(el.dataset.lang));
  });

  $("#sendBtn").addEventListener("click", handleSend);
  $("#newBtn").addEventListener("click", newSession);
  $("#input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) handleSend();
  });

  renderAll();
}
