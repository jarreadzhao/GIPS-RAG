/* =====================================================================
   api.js · 前后端联调 API 封装（GIPS 前端操作台 v1）

   单机部署无网关：所有请求自动注入 X-Role / X-Dept 让后端
   resolve_acl_scope() 按角色映射生成 acl_scope；POST /reviews
   自动生成 UUID 放入 Idempotency-Key。
   ===================================================================== */
const API_BASE = (typeof window !== "undefined" && window.__API_BASE__)
  ? window.__API_BASE__
  : "http://127.0.0.1:8011";

// 固定角色（单机前端无登录，硬塞默认）
const DEFAULT_ROLE = "reviewer";
const DEFAULT_DEPT = "compliance";

const NET_HINT = `无法连接后端（${API_BASE}）。请确认后端已启动：`
  + `python -m uvicorn app.main:app --port 8011`;

/** 生成 UUID（RFC 4122 v4 简易版，浏览器 crypto 不可用时退化为时间戳+随机）。 */
function uuid() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    const v = c === "x" ? r : (r & 0x3 | 0x8);
    return v.toString(16);
  });
}

/**
 * 统一请求入口。
 * @param {string} method   GET / POST / ...
 * @param {string} path     如 /provisions/search?q=1.A.4
 * @param {object} [body]   JSON 请求体（POST 时）
 * @param {object} [opts]   额外 fetch 选项（auth 头会被覆盖，别传 headers）
 * @returns {Promise<object|null>} 解析后的 JSON；204 返回 null
 * @throws {{kind:string, message:string, status?:number, detail?:any}}
 */
export async function apiFetch(method, path, body, opts = {}) {
  const headers = {
    "X-Role": DEFAULT_ROLE,
    "X-Dept": DEFAULT_DEPT,
    "Accept": "application/json",
  };
  if (body !== undefined && body !== null) {
    headers["Content-Type"] = "application/json";
  }
  // 审查接口强制要 Idempotency-Key，其他接口多带无害（服务端幂等键为空才拒绝）
  headers["Idempotency-Key"] = uuid();

  const init = {
    method,
    headers,
    ...opts,
    body: body ? JSON.stringify(body) : undefined,
  };

  let res;
  try {
    res = await fetch(API_BASE + path, init);
  } catch (first) {
    // 后端刚启动给一次宽容重试
    try {
      await new Promise(r => setTimeout(r, 300));
      res = await fetch(API_BASE + path, init);
    } catch (e) {
      throw { kind: "network", message: NET_HINT, raw: e };
    }
  }

  if (res.status === 204) return null;
  let data;
  try { data = await res.json(); } catch (_) { data = null; }

  if (!res.ok) {
    const msg = (data && (data.message || data.detail)) || `HTTP ${res.status}`;
    throw { kind: "http", status: res.status, message: msg, detail: data };
  }
  return data;
}

/** 健康检查：探测后端是否在线。 */
export async function healthz() {
  return apiFetch("GET", "/healthz");
}

/** 三通道检索（API-15）。 */
export async function searchProvisions({ q, channels, top_k = 20, lang }) {
  const params = new URLSearchParams();
  params.set("q", q);
  if (channels) params.set("channels", channels.join(","));
  if (top_k) params.set("top_k", top_k);
  if (lang) params.set("lang", lang);
  return apiFetch("GET", `/provisions/search?${params}`);
}

/** QA 问答（API-4，task_type=qa）。lang 决定答案与引用片段的语种。 */
export async function askQA({ text, history = [], report_year, scope = [],
                             lang = "zh" }) {
  const body = {
    task_type: "qa",
    supply: "self_hosted",
    text,
    history,
    scope,
    answer_lang: lang === "en" ? "en" : "zh",
  };
  if (report_year) body.report_year = report_year;
  return apiFetch("POST", "/reviews", body);
}

/** 取任务状态与 findings（API-5）。 */
export async function getReview(task_id) {
  return apiFetch("GET", `/reviews/${task_id}`);
}

/** 取单文档状态（API-19）。 */
export async function getDocument(doc_id) {
  return apiFetch("GET", `/documents/${doc_id}`);
}
