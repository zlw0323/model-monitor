"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const STATUS = {
  ok: { label: "正常", cls: "ok" },
  slow: { label: "TTFT 异常", cls: "warn" },
  down: { label: "连通失败", cls: "bad" },
  unknown: { label: "未检测", cls: "muted" },
};
const ALERT_KIND = { down: "连通失败", slow: "TTFT 异常", recovered: "恢复" };
const KIND = { text: "文本/推理", image: "图像" };

const state = { models: [], resources: [], editingId: null, editingResourceId: null, detailId: null, timer: null };

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function fmtTime(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function fmtShort(ms) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function fmtMs(v) {
  if (v == null) return "-";
  return v >= 10000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`;
}
function fmtPct(v) {
  return v == null ? "-" : `${v.toFixed(v === 100 ? 0 : 1)}%`;
}
function relTime(ts) {
  if (!ts) return "-";
  const diff = ts - Date.now() / 1000;
  const abs = Math.abs(diff);
  const s = abs < 60 ? `${Math.round(abs)} 秒` : abs < 3600 ? `${Math.round(abs / 60)} 分钟` : `${(abs / 3600).toFixed(1)} 小时`;
  return diff >= 0 ? `${s}后` : `${s}前`;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
  });
  if (res.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("未登录");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let msg = data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join("; ");
    throw new Error(msg || `请求失败 (${res.status})`);
  }
  return data;
}

// ---------- auth ----------
function showLogin() {
  stopTimer();
  $("#app-view").classList.add("hidden");
  $("#login-view").classList.remove("hidden");
}
function showApp() {
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  route();
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  $("#login-error").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: { username: f.get("username"), password: f.get("password") } });
    e.target.reset();
    showApp();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});
$("#logout-btn").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  showLogin();
});

// ---------- routing ----------
function route() {
  const tab = (location.hash || "#dashboard").slice(1);
  const valid = ["dashboard", "resources", "alerts", "settings"].includes(tab) ? tab : "dashboard";
  $$(".tab").forEach((el) => el.classList.toggle("hidden", el.id !== `tab-${valid}`));
  $$("nav a").forEach((a) => a.classList.toggle("active", a.dataset.tab === valid));
  stopTimer();
  if (valid === "dashboard") {
    renderResourceFilter();
    loadDashboard();
    state.timer = setInterval(loadDashboard, 15000);
  } else if (valid === "resources") {
    loadResources();
  } else if (valid === "alerts") {
    loadAlerts();
    state.timer = setInterval(loadAlerts, 15000);
  } else {
    loadSettings();
  }
}
function stopTimer() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
}
window.addEventListener("hashchange", () => {
  if (!$("#app-view").classList.contains("hidden")) route();
});

// ---------- dashboard (list view) ----------
const SEVERITY = { down: 0, slow: 1, unknown: 2, ok: 3, disabled: 4 };
const FILTER_KEY = "mm.filters.v1";
Object.assign(state, {
  resList: [],
  selected: new Set(),
  sort: { key: "status", dir: 1 },
  filter: { status: "", search: "", kind: "", resource: "", abnormal: false },
});
try {
  Object.assign(state.filter, JSON.parse(localStorage.getItem(FILTER_KEY) || "{}"));
} catch { /* ignore broken saved filters */ }

function saveFilters() {
  localStorage.setItem(FILTER_KEY, JSON.stringify(state.filter));
}
function effStatus(m) {
  return m.enabled ? m.status : "disabled";
}

async function loadDashboard() {
  let data;
  try {
    data = await api("/api/dashboard");
  } catch {
    return;
  }
  state.models = data.models;
  state.resList = data.resources;
  const ids = new Set(state.models.map((m) => m.id));
  for (const id of [...state.selected]) if (!ids.has(id)) state.selected.delete(id);
  renderResourceFilter();
  renderSummary();
  renderTable();
  if (state.detailId && $("#detail-dialog").open) refreshDetail();
}

function renderResourceFilter() {
  const sel = $("#f-resource");
  const opts = ['<option value="">全部资源</option>'].concat(
    state.resList.map((r) => `<option value="${r.id}">${esc(r.name)}（${r.model_count}）</option>`),
  );
  sel.innerHTML = opts.join("");
  if (state.filter.resource && !state.resList.some((r) => String(r.id) === String(state.filter.resource))) state.filter.resource = "";
  sel.value = state.filter.resource;
}

function renderSummary() {
  const counts = { "": state.models.length, ok: 0, slow: 0, down: 0, unknown: 0, disabled: 0 };
  for (const m of state.models) counts[effStatus(m)]++;
  const chip = (key, label, cls) =>
    `<button class="chip ${cls} ${state.filter.status === key ? "active" : ""}" data-status="${key}">${label} <b>${counts[key]}</b></button>`;
  $("#summary").innerHTML = [
    chip("", "全部", ""), chip("ok", "正常", "ok"), chip("slow", "TTFT 异常", "warn"),
    chip("down", "连通失败", "bad"), chip("unknown", "未检测", "muted"), chip("disabled", "已停用", "muted"),
  ].join("");
}

function filteredModels() {
  const f = state.filter;
  const q = f.search.trim().toLowerCase();
  return state.models.filter((m) => {
    const st = effStatus(m);
    if (f.status && st !== f.status) return false;
    if (f.abnormal && st !== "down" && st !== "slow") return false;
    if (f.kind && m.kind !== f.kind) return false;
    if (f.resource && String(m.resource_id) !== String(f.resource)) return false;
    if (q && !`${m.name} ${m.deployment} ${m.resource_name}`.toLowerCase().includes(q)) return false;
    return true;
  });
}

function sortModels(list) {
  const { key, dir } = state.sort;
  const nullsLast = (a, b) => (a == null) - (b == null);
  const cmp = {
    status: (a, b) => SEVERITY[effStatus(a)] - SEVERITY[effStatus(b)],
    name: (a, b) => a.name.localeCompare(b.name, "zh"),
    resource: (a, b) => a.resource_name.localeCompare(b.resource_name, "zh"),
    ttft: (a, b) => nullsLast(a.latest?.ttft_ms, b.latest?.ttft_ms) || (a.latest?.ttft_ms ?? 0) - (b.latest?.ttft_ms ?? 0),
    avail: (a, b) => nullsLast(a.availability_24h, b.availability_24h) || (a.availability_24h ?? 0) - (b.availability_24h ?? 0),
    last: (a, b) => (b.last_check_at ?? 0) - (a.last_check_at ?? 0),
  }[key];
  return [...list].sort((a, b) => dir * cmp(a, b) || a.name.localeCompare(b.name, "zh"));
}

function baselineText(m) {
  if (m.baseline_source === "manual") return `${fmtMs(m.baseline_ms)}（手动）`;
  if (m.baseline_source === "auto") return fmtMs(m.baseline_ms);
  return `<span class="muted">学习中 ${m.baseline_samples}/${m.baseline_min_samples}</span>`;
}

function sparkSvg(m) {
  const pts = m.spark || [];
  const W = 120, H = 28, P = 2;
  if (!pts.length) return '<span class="muted small">-</span>';
  const t0 = pts[0][0], t1 = Math.max(pts[pts.length - 1][0], t0 + 1);
  const threshold = m.baseline_ms ? m.baseline_ms * (1 + m.effective_threshold_pct / 100) : null;
  const yMax = Math.max(1, ...pts.map((p) => p[1] || 0), threshold || 0);
  const x = (t) => (P + ((t - t0) / (t1 - t0)) * (W - 2 * P)).toFixed(1);
  const y = (v) => (H - P - (v / yMax) * (H - 2 * P)).toFixed(1);
  const okPts = pts.filter((p) => p[2] !== 2);
  let svg = `<svg class="spark" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true">`;
  if (threshold) svg += `<line x1="${P}" x2="${W - P}" y1="${y(threshold)}" y2="${y(threshold)}" class="spark-th"/>`;
  if (okPts.length > 1) svg += `<polyline class="spark-line" points="${okPts.map((p) => `${x(p[0])},${y(p[1])}`).join(" ")}"/>`;
  for (const p of pts) {
    if (p[2] === 2) svg += `<rect class="spark-fail" x="${(x(p[0]) - 1.5).toFixed(1)}" y="${H - 6}" width="3" height="4"/>`;
    else if (p[2] === 1) svg += `<circle class="spark-anom" cx="${x(p[0])}" cy="${y(p[1])}" r="2"/>`;
  }
  if (okPts.length === 1) svg += `<circle class="spark-dot" cx="${x(okPts[0][0])}" cy="${y(okPts[0][1])}" r="1.8"/>`;
  return svg + "</svg>";
}

function statusCell(m) {
  const st = effStatus(m);
  const badge = st === "disabled" ? '<span class="badge muted">已停用</span>'
    : `<span class="badge ${(STATUS[st] || STATUS.unknown).cls}">${(STATUS[st] || STATUS.unknown).label}</span>`;
  const activity = m.queued ? '<span class="badge muted" title="等待其他检测完成（并发上限或独占重测）">排队中</span>'
    : m.running ? '<span class="badge info">检测中</span>' : "";
  return `${badge}${activity}`;
}

function ttftCell(m) {
  const l = m.latest;
  if (!l) return "-";
  if (!l.success) return `<span class="badge bad" title="${esc(l.error || "")}">失败</span>`;
  return `<span class="${l.anomaly ? "warn-text" : ""}" title="${l.interfered ? "与图像检测同时进行（受干扰）" : ""}">${fmtMs(l.ttft_ms)}${l.interfered ? "*" : ""}</span>`;
}

function renderTable() {
  const list = sortModels(filteredModels());
  const empty = $("#empty-hint");
  if (!state.models.length) {
    empty.textContent = "还没有模型。先在“资源”页添加 Azure 资源，再点击“添加模型”或“批量导入”。";
  } else if (!list.length) {
    empty.textContent = "没有符合筛选条件的模型。";
  }
  empty.classList.toggle("hidden", list.length > 0);
  $$(".models-table th[data-sort]").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sort === state.sort.key);
    th.dataset.dir = th.dataset.sort === state.sort.key ? (state.sort.dir > 0 ? "asc" : "desc") : "";
  });
  $("#model-body").innerHTML = list.map((m) => {
    const th = m.baseline_ms ? m.baseline_ms * (1 + m.effective_threshold_pct / 100) : null;
    const sel = state.selected.has(m.id);
    return `<tr data-id="${m.id}" class="${sel ? "selected" : ""} ${m.enabled ? "" : "row-disabled"}">
      <td class="col-check"><input type="checkbox" data-id="${m.id}" ${sel ? "checked" : ""} aria-label="选择"></td>
      <td class="nowrap">${statusCell(m)}</td>
      <td><div class="model-name">${esc(m.name)}</div><div class="muted small">${esc(m.deployment)}</div></td>
      <td>${esc(m.resource_name)}</td>
      <td class="nowrap">${KIND[m.kind]}</td>
      <td class="num nowrap">${ttftCell(m)}</td>
      <td class="num nowrap">${baselineText(m)}<div class="muted small">${th ? `> ${fmtMs(th)}` : ""}</div></td>
      <td class="num">${fmtPct(m.availability_24h)}</td>
      <td class="num">${fmtPct(m.availability_7d)}</td>
      <td>${sparkSvg(m)}</td>
      <td class="small nowrap" title="${fmtTime(m.last_check_at)}">${relTime(m.last_check_at)}<div class="muted">${m.enabled ? `下次 ${relTime(m.next_run_at)}` : ""}</div></td>
      <td class="col-actions nowrap">
        <button class="btn small" data-act="run">检测</button><button class="btn small" data-act="detail">详情</button><button class="btn small" data-act="edit">编辑</button><button class="btn small danger" data-act="delete">删除</button>
      </td></tr>`;
  }).join("");
  syncBulkBar(list);
}

function syncBulkBar(list = sortModels(filteredModels())) {
  const n = state.selected.size;
  $("#bulk-bar").classList.toggle("hidden", n === 0);
  $("#bulk-count").textContent = `已选 ${n} 个模型`;
  const visible = list.map((m) => m.id);
  const checkedVisible = visible.filter((id) => state.selected.has(id)).length;
  const all = $("#m-check-all");
  all.checked = visible.length > 0 && checkedVisible === visible.length;
  all.indeterminate = checkedVisible > 0 && checkedVisible < visible.length;
}

// filters / sorting / selection
$("#summary").addEventListener("click", (e) => {
  const b = e.target.closest("[data-status]");
  if (!b) return;
  state.filter.status = state.filter.status === b.dataset.status ? "" : b.dataset.status;
  saveFilters();
  renderSummary();
  renderTable();
});
for (const [id, key, ev] of [["#f-search", "search", "input"], ["#f-kind", "kind", "change"], ["#f-resource", "resource", "change"]]) {
  $(id).value = state.filter[key];
  $(id).addEventListener(ev, (e) => {
    state.filter[key] = e.target.value;
    saveFilters();
    renderTable();
  });
}
$("#f-abnormal").checked = state.filter.abnormal;
$("#f-abnormal").addEventListener("change", (e) => {
  state.filter.abnormal = e.target.checked;
  saveFilters();
  renderTable();
});
$(".models-table thead").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-sort]");
  if (!th) return;
  const key = th.dataset.sort;
  state.sort = { key, dir: state.sort.key === key ? -state.sort.dir : 1 };
  renderTable();
});
$("#m-check-all").addEventListener("change", (e) => {
  for (const m of filteredModels()) e.target.checked ? state.selected.add(m.id) : state.selected.delete(m.id);
  renderTable();
});
$("#bulk-clear").addEventListener("click", () => {
  state.selected.clear();
  renderTable();
});
$("#bulk-bar").addEventListener("click", async (e) => {
  const action = e.target.dataset.bulk;
  if (!action) return;
  const ids = [...state.selected];
  const label = { run: "立即检测", enable: "启用", disable: "停用", delete: "删除" }[action];
  const warn = action === "delete" ? "\n删除会同时清除这些模型的全部历史数据，不可恢复。" : "";
  if (!confirm(`确定对选中的 ${ids.length} 个模型执行「${label}」？${warn}`)) return;
  try {
    const r = await api("/api/models/bulk", { method: "POST", body: { ids, action } });
    if (action === "delete") state.selected.clear();
    if (action === "run" && r.affected < r.matched) alert(`已触发 ${r.affected} 个；其余模型已停用或正在检测中。`);
  } catch (err) {
    alert(err.message);
  }
  loadDashboard();
});
$("#model-body").addEventListener("change", (e) => {
  const id = Number(e.target.dataset.id);
  if (!id) return;
  e.target.checked ? state.selected.add(id) : state.selected.delete(id);
  e.target.closest("tr").classList.toggle("selected", e.target.checked);
  syncBulkBar();
});
$("#model-body").addEventListener("click", async (e) => {
  const act = e.target.closest("button")?.dataset.act;
  if (!act) return;
  const id = Number(e.target.closest("tr").dataset.id);
  const m = state.models.find((x) => x.id === id);
  if (act === "run") {
    try {
      await api(`/api/models/${id}/run`, { method: "POST" });
    } catch (err) {
      alert(err.message);
    }
    loadDashboard();
  } else if (act === "edit") {
    openModelDialog(m);
  } else if (act === "delete") {
    if (confirm(`确定删除「${m.name}」及其全部历史数据？`)) {
      await api(`/api/models/${id}`, { method: "DELETE" }).catch((err) => alert(err.message));
      state.selected.delete(id);
      loadDashboard();
    }
  } else if (act === "detail") {
    state.detailId = id;
    $("#detail-dialog").showModal();
    refreshDetail();
  }
});

// ---------- detail ----------
function chartConfig(m, checks) {
  const okPts = [], anomalyPts = [], failPts = [];
  for (const c of checks) {
    const x = c.ts * 1000;
    if (!c.success) failPts.push({ x, y: 0, c });
    else if (c.anomaly) anomalyPts.push({ x, y: c.ttft_ms, c });
    else okPts.push({ x, y: c.ttft_ms, c });
  }
  const line = [...okPts, ...anomalyPts].sort((a, b) => a.x - b.x);
  const xs = checks.map((c) => c.ts * 1000);
  const xMin = xs.length ? Math.min(...xs) : Date.now() - 3600e3;
  const xMax = xs.length ? Math.max(...xs) : Date.now();
  const datasets = [
    { type: "line", label: "TTFT", data: line, borderColor: "#3b82f6", backgroundColor: "#3b82f6", pointRadius: 3, borderWidth: 1.5, tension: 0.2 },
    { type: "scatter", label: "TTFT 异常", data: anomalyPts, backgroundColor: "#f59e0b", pointRadius: 5 },
    { type: "scatter", label: "失败", data: failPts, backgroundColor: "#ef4444", pointStyle: "crossRot", borderColor: "#ef4444", pointRadius: 6, borderWidth: 2 },
  ];
  if (m.baseline_ms) {
    datasets.push({ type: "line", label: "基线", data: [{ x: xMin, y: m.baseline_ms }, { x: xMax, y: m.baseline_ms }], borderColor: "#10b981", borderDash: [4, 4], pointRadius: 0, borderWidth: 1 });
    const th = m.baseline_ms * (1 + m.effective_threshold_pct / 100);
    datasets.push({ type: "line", label: "预警值", data: [{ x: xMin, y: th }, { x: xMax, y: th }], borderColor: "#f97316", borderDash: [2, 3], pointRadius: 0, borderWidth: 1 });
  }
  return {
    data: { datasets },
    options: {
      animation: false,
      maintainAspectRatio: false,
      parsing: false,
      scales: {
        x: { type: "linear", min: xMin, max: xMax, ticks: { callback: (v) => fmtShort(v), maxTicksLimit: 8, font: { size: 10 } } },
        y: { beginAtZero: true, ticks: { callback: (v) => fmtMs(v), font: { size: 10 } } },
      },
      plugins: {
        legend: { display: true, labels: { boxWidth: 10 } },
        tooltip: {
          callbacks: {
            title: (items) => fmtTime(items[0].raw.x / 1000),
            label: (item) => {
              const c = item.raw.c;
              if (!c) return `${item.dataset.label}: ${fmtMs(item.raw.y)}`;
              const tag = (c.attempt ? `第${c.attempt}次重测` : "首次") + (c.exclusive ? "·独占" : "") + (c.interfered ? "·受干扰" : "");
              return c.success ? `${tag} TTFT ${fmtMs(c.ttft_ms)}` : `${tag} 失败: ${(c.error || "").slice(0, 80)}`;
            },
          },
        },
      },
    },
  };
}

let detailChart = null;
async function refreshDetail() {
  const m = state.models.find((x) => x.id === state.detailId);
  if (!m) return;
  const hours = Number($("#detail-range").value);
  $("#detail-title").textContent = m.name;
  $("#detail-meta").innerHTML = `${KIND[m.kind]} · 资源 ${esc(m.resource_name)} · 部署 ${esc(m.deployment)}<br>请求地址：<code>${esc(m.request_url)}</code>${m.kind === "text" ? ` · token 参数：<code>${esc(m.token_param)}</code>` : ""}`;
  const checks = await api(`/api/models/${m.id}/checks?hours=${hours}`).catch(() => []);
  if (detailChart) detailChart.destroy();
  detailChart = new Chart($("#detail-chart"), chartConfig(m, checks));
  const trig = { schedule: "定时", manual: "手动" };
  $("#detail-body").innerHTML = [...checks].reverse().slice(0, 300).map((c) => {
    const res = !c.success ? '<span class="badge bad">失败</span>' : c.anomaly ? '<span class="badge warn">超标</span>' : '<span class="badge ok">正常</span>';
    const round = (c.attempt ? `重测 ${c.attempt}` : "首次") + (c.exclusive ? ' <span class="badge info" title="暂停其他检测后单独测量">独占</span>' : "");
    const conc = c.concurrent == null ? "-"
      : c.interfered ? `<span class="badge warn" title="与图像检测同时进行，不计入基线">${c.concurrent} · 受干扰</span>`
      : String(c.concurrent);
    return `<tr><td>${fmtTime(c.ts)}</td><td>${trig[c.trigger] || c.trigger}</td><td>${round}</td><td>${res}</td>
      <td>${fmtMs(c.ttft_ms)}</td><td>${fmtMs(c.total_ms)}</td><td>${conc}</td><td>${c.http_status ?? "-"}</td><td class="err-cell" title="${esc(c.error)}">${esc(c.error || "")}</td></tr>`;
  }).join("") || '<tr><td colspan="9" class="muted">暂无数据</td></tr>';
}
$("#detail-range").addEventListener("change", refreshDetail);
$("#detail-close").addEventListener("click", () => {
  $("#detail-dialog").close();
  state.detailId = null;
});
$("#detail-reset-baseline").addEventListener("click", async () => {
  if (!confirm("重置后将只使用之后的检测结果重新学习自动基线（手动基线不受影响）。继续？")) return;
  await api(`/api/models/${state.detailId}/reset-baseline`, { method: "POST" }).catch((err) => alert(err.message));
  loadDashboard();
});

// ---------- model form ----------
async function openModelDialog(m) {
  const resources = await api("/api/resources").catch(() => []);
  if (!resources.length) {
    alert("请先在“资源”页添加 Azure 资源（端点 + API Key），再添加模型。");
    location.hash = "#resources";
    return;
  }
  const form = $("#model-form");
  form.reset();
  $("#model-form-error").textContent = "";
  state.editingId = m ? m.id : null;
  $("#model-dialog-title").textContent = m ? "编辑模型" : "添加模型";
  form.resource_id.innerHTML = resources.map((r) => `<option value="${r.id}">${esc(r.name)}</option>`).join("");
  if (m) {
    for (const k of ["name", "kind", "deployment", "interval_minutes", "resource_id"]) form[k].value = m[k];
    form.ttft_threshold_pct.value = m.ttft_threshold_pct ?? "";
    form.baseline_manual_ms.value = m.baseline_manual_ms ?? "";
    form.endpoint_override.value = m.endpoint_override ?? "";
    form.enabled.checked = m.enabled;
    $(".advanced", form).open = Boolean(m.endpoint_override);
  } else {
    const s = await api("/api/settings").catch(() => ({}));
    form.interval_minutes.value = s.default_interval_minutes || 30;
    form.enabled.checked = true;
    if (state.filter.resource) form.resource_id.value = state.filter.resource;
    $(".advanced", form).open = false;
  }
  $("#model-dialog").showModal();
}
$("#add-model-btn").addEventListener("click", () => openModelDialog(null));
$("#model-cancel").addEventListener("click", () => $("#model-dialog").close());
$("#model-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const num = (v) => (v === "" ? null : Number(v));
  const body = {
    name: f.name.value,
    kind: f.kind.value,
    resource_id: Number(f.resource_id.value),
    deployment: f.deployment.value,
    endpoint_override: f.endpoint_override.value.trim() || null,
    enabled: f.enabled.checked,
    interval_minutes: Number(f.interval_minutes.value),
    ttft_threshold_pct: num(f.ttft_threshold_pct.value),
    baseline_manual_ms: num(f.baseline_manual_ms.value),
  };
  try {
    if (state.editingId) await api(`/api/models/${state.editingId}`, { method: "PUT", body });
    else await api("/api/models", { method: "POST", body });
    $("#model-dialog").close();
    loadDashboard();
  } catch (err) {
    $("#model-form-error").textContent = err.message;
  }
});
$("#model-form").deployment.addEventListener("input", (e) => {
  const name = $("#model-form").name;
  if (!state.editingId && (!name.value || name.dataset.auto === "1")) {
    name.value = e.target.value;
    name.dataset.auto = "1";
  }
});
$("#model-form").name.addEventListener("input", (e) => (e.target.dataset.auto = "0"));

// ---------- import ----------
let importPreviewed = false;
$("#import-btn").addEventListener("click", () => {
  $("#import-msg").textContent = "";
  $("#import-body").innerHTML = "";
  $("#import-run-btn").disabled = true;
  importPreviewed = false;
  $("#import-dialog").showModal();
});
$("#import-close").addEventListener("click", () => $("#import-dialog").close());
$("#import-text").addEventListener("input", () => {
  importPreviewed = false;
  $("#import-run-btn").disabled = true;
});
$("#import-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  if (file.size > 500_000) return alert("文件过大（上限 500KB）");
  const buf = await file.arrayBuffer();
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(buf);
  } catch {
    text = new TextDecoder("gbk").decode(buf); // Excel on Chinese Windows saves CSV as GBK
  }
  $("#import-text").value = text;
  $("#import-text").dispatchEvent(new Event("input"));
  runImport(true);
});
$("#import-preview-btn").addEventListener("click", () => runImport(true));
$("#import-run-btn").addEventListener("click", () => {
  if (importPreviewed) runImport(false);
});
async function runImport(dryRun) {
  const msg = $("#import-msg");
  msg.className = "small";
  msg.textContent = dryRun ? "解析中…" : "导入中…";
  try {
    const r = await api("/api/models/import", { method: "POST", body: { text: $("#import-text").value, dry_run: dryRun } });
    const s = r.summary;
    const label = { ok: '<span class="badge ok">可导入</span>', duplicate: '<span class="badge muted">跳过</span>', error: '<span class="badge bad">错误</span>' };
    $("#import-body").innerHTML = r.rows.map((x) => `<tr><td>${x.line}</td><td>${label[x.status]}</td><td>${esc(x.resource)}</td><td>${esc(x.name)}</td>
      <td>${KIND[x.kind] || esc(x.kind)}</td><td>${esc(x.deployment)}</td><td>${x.interval ?? ""}</td><td>${x.threshold ?? ""}</td><td class="small">${esc(x.message)}</td></tr>`).join("");
    if (dryRun) {
      importPreviewed = true;
      $("#import-run-btn").disabled = s.ok === 0;
      $("#import-run-btn").textContent = `导入 ${s.ok} 个`;
      msg.textContent = `可导入 ${s.ok} 个，跳过 ${s.duplicate} 个重复，${s.error} 行有错误${s.error ? "（有错误的行不会导入）" : ""}。`;
    } else {
      importPreviewed = false;
      $("#import-run-btn").disabled = true;
      msg.className = "small success";
      msg.textContent = `已导入 ${r.created} 个模型，首次检测会在 5 分钟内陆续开始。`;
      loadDashboard();
    }
  } catch (err) {
    msg.className = "small error";
    msg.textContent = err.message;
  }
}

// ---------- resources ----------
async function loadResources() {
  const [resources, dash] = await Promise.all([api("/api/resources").catch(() => []), api("/api/dashboard?spark_hours=1").catch(() => null)]);
  if (dash) state.models = dash.models;
  state.resources = resources;
  $("#resource-body").innerHTML = resources.map((r) => {
    const ms = state.models.filter((m) => m.resource_id === r.id);
    const n = (st) => ms.filter((m) => effStatus(m) === st).length;
    const stat = [["ok", "ok", "正常"], ["slow", "warn", "TTFT 异常"], ["down", "bad", "失败"]]
      .filter(([st]) => n(st)).map(([st, cls, lbl]) => `<span class="badge ${cls}">${lbl} ${n(st)}</span>`).join(" ") || '<span class="muted">-</span>';
    return `<tr data-id="${r.id}"><td><b>${esc(r.name)}</b></td><td><code>${esc(r.endpoint)}</code></td><td><code>${esc(r.api_key_masked)}</code></td>
      <td class="num">${r.model_count}</td><td>${stat}</td>
      <td class="col-actions nowrap"><button class="btn small" data-act="models">查看模型</button><button class="btn small" data-act="edit">编辑</button><button class="btn small danger" data-act="delete">删除</button></td></tr>`;
  }).join("") || '<tr><td colspan="6" class="muted empty-row">还没有资源。点击右上角“添加资源”，填写 Foundry 资源的端点和 API Key。</td></tr>';
}
$("#resource-body").addEventListener("click", async (e) => {
  const act = e.target.closest("button")?.dataset.act;
  if (!act) return;
  const id = Number(e.target.closest("tr").dataset.id);
  const r = state.resources.find((x) => x.id === id);
  if (act === "models") {
    Object.assign(state.filter, { resource: String(id), status: "", abnormal: false, search: "" });
    saveFilters();
    $("#f-search").value = "";
    $("#f-abnormal").checked = false;
    location.hash = "#dashboard";
  } else if (act === "edit") {
    openResourceDialog(r);
  } else if (act === "delete") {
    if (!confirm(`确定删除资源「${r.name}」？`)) return;
    try {
      await api(`/api/resources/${id}`, { method: "DELETE" });
    } catch (err) {
      alert(err.message);
    }
    loadResources();
  }
});
function openResourceDialog(r) {
  const f = $("#resource-form");
  f.reset();
  $("#resource-form-error").textContent = "";
  state.editingResourceId = r ? r.id : null;
  $("#resource-dialog-title").textContent = r ? "编辑资源" : "添加资源";
  f.api_key.required = !r;
  f.api_key.placeholder = r ? `已保存 ${r.api_key_masked}，留空保持不变` : "";
  if (r) {
    f.name.value = r.name;
    f.endpoint.value = r.endpoint;
  }
  $("#resource-dialog").showModal();
}
$("#add-resource-btn").addEventListener("click", () => openResourceDialog(null));
$("#resource-cancel").addEventListener("click", () => $("#resource-dialog").close());
$("#resource-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = { name: f.name.value, endpoint: f.endpoint.value, api_key: f.api_key.value || null };
  try {
    if (state.editingResourceId) await api(`/api/resources/${state.editingResourceId}`, { method: "PUT", body });
    else await api("/api/resources", { method: "POST", body });
    $("#resource-dialog").close();
    loadResources();
  } catch (err) {
    $("#resource-form-error").textContent = err.message;
  }
});

// ---------- alerts ----------
let alertsCache = [];
function selectedAlertIds() {
  return $$("#alerts-body input[data-id]:checked").map((el) => Number(el.dataset.id));
}
function syncAlertSelection() {
  const boxes = $$("#alerts-body input[data-id]");
  boxes.forEach((b) => b.closest("tr").classList.toggle("selected", b.checked));
  const checked = boxes.filter((b) => b.checked).length;
  $("#alerts-delete-selected").disabled = checked === 0;
  $("#alerts-delete-selected").textContent = checked ? `删除选中 (${checked})` : "删除选中";
  $("#alerts-check-all").checked = boxes.length > 0 && checked === boxes.length;
  $("#alerts-check-all").indeterminate = checked > 0 && checked < boxes.length;
}
function deliveryBadge(status) {
  if (!status) return '<span class="badge muted">-</span>';
  const s = String(status);
  const [cls, label] = s.startsWith("已发送") ? ["ok", "已发送"]
    : s.startsWith("已补发") ? ["ok", "已补发"]
    : s.startsWith("未发送") ? ["muted", "未配置"]
    : s.startsWith("发送中") ? ["info", "发送中"]
    : s.includes("将自动重试") ? ["warn", "待重试"]
    : ["bad", "失败"];
  return `<span class="badge ${cls}" title="${esc(s)}">${label}</span>`;
}
async function loadAlerts() {
  const keep = new Set(selectedAlertIds());
  alertsCache = await api("/api/alerts").catch(() => alertsCache);
  $("#alerts-body").innerHTML = alertsCache.map((a, i) => {
    const cls = a.kind === "recovered" ? "ok" : a.kind === "slow" ? "warn" : "bad";
    return `<tr data-row="${a.id}"><td class="col-check"><input type="checkbox" data-id="${a.id}" aria-label="选择"></td>
      <td class="nowrap">${fmtTime(a.ts)}</td><td class="nowrap">${esc(a.model_name || "(已删除)")}</td>
      <td><span class="badge ${cls}">${ALERT_KIND[a.kind] || a.kind}</span></td>
      <td class="col-subject" title="${esc(a.subject)}">${esc(a.subject)}</td>
      <td>${deliveryBadge(a.email_status)}</td><td>${deliveryBadge(a.feishu_status)}</td>
      <td class="nowrap col-actions"><button class="btn small" data-view="${i}">查看</button><button class="btn small danger" data-del="${a.id}">删除</button></td></tr>`;
  }).join("") || '<tr><td colspan="8" class="muted empty-row">暂无报警</td></tr>';
  $$("#alerts-body input[data-id]").forEach((b) => (b.checked = keep.has(Number(b.dataset.id))));
  $("#alerts-clear").disabled = alertsCache.length === 0;
  $("#alerts-check-all").disabled = alertsCache.length === 0;
  syncAlertSelection();
}
async function deleteAlerts(body, confirmText) {
  if (!confirm(confirmText)) return;
  try {
    await api("/api/alerts/delete", { method: "POST", body });
  } catch (err) {
    alert(err.message);
  }
  loadAlerts();
}
$("#alerts-body").addEventListener("change", syncAlertSelection);
$("#alerts-check-all").addEventListener("change", (e) => {
  $$("#alerts-body input[data-id]").forEach((b) => (b.checked = e.target.checked));
  syncAlertSelection();
});
$("#alerts-delete-selected").addEventListener("click", () => {
  const ids = selectedAlertIds();
  if (ids.length) deleteAlerts({ ids }, `确定删除选中的 ${ids.length} 条报警记录？`);
});
$("#alerts-clear").addEventListener("click", () => deleteAlerts({ all: true }, "确定清空全部报警记录？此操作不可恢复。"));
$("#alerts-body").addEventListener("click", (e) => {
  const { view, del } = e.target.dataset;
  if (del != null) {
    deleteAlerts({ ids: [Number(del)] }, "确定删除这条报警记录？");
    return;
  }
  if (view == null) return;
  const a = alertsCache[Number(view)];
  $("#alert-title").textContent = a.subject;
  $("#alert-message").textContent = a.message;
  $("#alert-dialog").showModal();
});
$("#alert-close").addEventListener("click", () => $("#alert-dialog").close());

// ---------- settings ----------
async function loadSettings() {
  const s = await api("/api/settings").catch(() => null);
  if (!s) return;
  for (const form of [$("#monitor-settings-form"), $("#smtp-settings-form")]) {
    for (const el of form.elements) {
      if (el.name && el.name in s && el.type !== "checkbox") el.value = s[el.name];
    }
  }
  const smtp = $("#smtp-settings-form");
  smtp.smtp_password.value = "";
  smtp.smtp_password.placeholder = s.smtp_password_set ? "已保存，留空保持不变" : "未设置";
  smtp.smtp_password_clear.checked = false;
  const fs = $("#feishu-settings-form");
  fs.feishu_webhook.value = "";
  fs.feishu_webhook.placeholder = s.feishu_webhook_masked ? `已保存 ${s.feishu_webhook_masked}，留空保持不变` : "https://open.feishu.cn/open-apis/bot/v2/hook/...";
  fs.feishu_secret.value = "";
  fs.feishu_secret.placeholder = s.feishu_secret_set ? "已保存，留空保持不变" : "未设置（机器人开启签名校验时必填）";
  fs.feishu_webhook_clear.checked = false;
  fs.feishu_secret_clear.checked = false;
}
function flash(form, text, isError) {
  const el = $(".msg", form);
  el.textContent = text;
  el.className = `msg ${isError ? "error" : "success"}`;
  setTimeout(() => (el.textContent = ""), 5000);
}
$("#monitor-settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const values = {};
  for (const el of e.target.elements) if (el.name) values[el.name] = el.value;
  try {
    await api("/api/settings", { method: "PUT", body: { values } });
    flash(e.target, "已保存");
  } catch (err) {
    flash(e.target, err.message, true);
  }
});
$("#smtp-settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const values = {};
  for (const k of ["smtp_host", "smtp_port", "smtp_security", "smtp_username", "smtp_from", "smtp_to"]) values[k] = f[k].value;
  try {
    await api("/api/settings", {
      method: "PUT",
      body: { values, smtp_password: f.smtp_password.value || null, smtp_password_clear: f.smtp_password_clear.checked },
    });
    await loadSettings();
    flash(f, "已保存");
  } catch (err) {
    flash(f, err.message, true);
  }
});
$("#test-email-btn").addEventListener("click", async () => {
  const f = $("#smtp-settings-form");
  flash(f, "发送中…");
  try {
    await api("/api/settings/test-email", { method: "POST" });
    flash(f, "测试邮件已发送，请查收");
  } catch (err) {
    flash(f, err.message, true);
  }
});
$("#feishu-settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    await api("/api/settings", {
      method: "PUT",
      body: {
        feishu_webhook: f.feishu_webhook.value.trim() || null,
        feishu_webhook_clear: f.feishu_webhook_clear.checked,
        feishu_secret: f.feishu_secret.value.trim() || null,
        feishu_secret_clear: f.feishu_secret_clear.checked,
      },
    });
    await loadSettings();
    flash(f, "已保存");
  } catch (err) {
    flash(f, err.message, true);
  }
});
$("#test-feishu-btn").addEventListener("click", async () => {
  const f = $("#feishu-settings-form");
  flash(f, "发送中…");
  try {
    await api("/api/settings/test-feishu", { method: "POST" });
    flash(f, "测试消息已发送，请在飞书群中查看");
  } catch (err) {
    flash(f, err.message, true);
  }
});

// ---------- boot ----------
api("/api/me").then(showApp).catch(() => showLogin());
