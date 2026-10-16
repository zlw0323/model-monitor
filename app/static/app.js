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

const state = { models: [], charts: new Map(), editingId: null, detailId: null, timer: null };

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
  const valid = ["dashboard", "alerts", "settings"].includes(tab) ? tab : "dashboard";
  $$(".tab").forEach((el) => el.classList.toggle("hidden", el.id !== `tab-${valid}`));
  $$("nav a").forEach((a) => a.classList.toggle("active", a.dataset.tab === valid));
  stopTimer();
  if (valid === "dashboard") {
    loadDashboard();
    state.timer = setInterval(loadDashboard, 15000);
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

// ---------- dashboard ----------
async function loadDashboard() {
  let models;
  try {
    models = await api("/api/models");
  } catch {
    return;
  }
  state.models = models;
  renderSummary(models);
  renderGrid(models);
  const hours = Number($("#range-select").value);
  await Promise.all(models.map((m) => loadCardChart(m, hours)));
  if (state.detailId && $("#detail-dialog").open) refreshDetail();
}

function renderSummary(models) {
  const count = (s) => models.filter((m) => m.enabled && m.status === s).length;
  $("#summary").innerHTML = `
    <span>共 <b>${models.length}</b> 个模型</span>
    <span class="dot ok"></span>正常 <b>${count("ok")}</b>
    <span class="dot warn"></span>TTFT 异常 <b>${count("slow")}</b>
    <span class="dot bad"></span>连通失败 <b>${count("down")}</b>
    <span class="dot muted"></span>未检测 <b>${count("unknown")}</b>`;
}

function baselineText(m) {
  if (m.baseline_source === "manual") return `${fmtMs(m.baseline_ms)}（手动）`;
  if (m.baseline_source === "auto") return `${fmtMs(m.baseline_ms)}（自动）`;
  return `学习中 ${m.baseline_samples}/${m.baseline_min_samples}`;
}

function renderGrid(models) {
  $("#empty-hint").classList.toggle("hidden", models.length > 0);
  const grid = $("#model-grid");
  const ids = new Set(models.map((m) => m.id));
  for (const [id, chart] of state.charts) {
    if (!ids.has(id)) {
      chart.destroy();
      state.charts.delete(id);
    }
  }
  $$(".model-card", grid).forEach((el) => {
    if (!ids.has(Number(el.dataset.id))) el.remove();
  });
  for (const m of models) {
    let card = $(`.model-card[data-id="${m.id}"]`, grid);
    if (!card) {
      card = document.createElement("div");
      card.className = "card model-card";
      card.dataset.id = m.id;
      card.innerHTML = `<div class="card-body"></div><div class="chart-wrap"><canvas></canvas></div>
        <div class="card-actions">
          <button class="btn small" data-act="run">立即检测</button>
          <button class="btn small" data-act="detail">详情</button>
          <button class="btn small" data-act="edit">编辑</button>
          <button class="btn small danger" data-act="delete">删除</button>
        </div>`;
      card.addEventListener("click", (e) => onCardAction(e, m.id));
      grid.appendChild(card);
    }
    const st = STATUS[m.status] || STATUS.unknown;
    const latest = m.latest;
    const thresholdMs = m.baseline_ms ? m.baseline_ms * (1 + m.effective_threshold_pct / 100) : null;
    const errorLine = latest && !latest.success && latest.error ? `<div class="err" title="${esc(latest.error)}">${esc(latest.error)}</div>` : "";
    $(".card-body", card).innerHTML = `
      <div class="card-head">
        <div>
          <div class="model-name">${esc(m.name)}</div>
          <div class="muted small">${KIND[m.kind]} · ${esc(m.deployment)}</div>
        </div>
        <div class="badges">
          ${m.queued ? '<span class="badge muted" title="等待其他检测完成（并发上限或独占重测）">排队中…</span>' : m.running ? '<span class="badge info">检测中…</span>' : ""}
          ${m.enabled ? `<span class="badge ${st.cls}">${st.label}</span>` : '<span class="badge muted">已停用</span>'}
        </div>
      </div>
      <div class="metrics">
        <div><label>最新 TTFT</label><b class="${latest && latest.anomaly ? "warn-text" : ""}">${latest ? fmtMs(latest.ttft_ms) : "-"}</b></div>
        <div><label>基线</label><b>${baselineText(m)}</b></div>
        <div><label>预警值</label><b>${thresholdMs ? `> ${fmtMs(thresholdMs)}` : "-"}</b><span class="muted small"> +${m.effective_threshold_pct}%</span></div>
        <div><label>24h 可用率</label><b>${fmtPct(m.availability_24h)}</b></div>
        <div><label>7d 可用率</label><b>${fmtPct(m.availability_7d)}</b></div>
        <div><label>间隔</label><b>${m.interval_minutes} 分钟</b></div>
      </div>
      <div class="muted small">上次检测：${fmtTime(m.last_check_at)}（${relTime(m.last_check_at)}） · 下次：${m.enabled ? relTime(m.next_run_at) : "-"}</div>
      ${errorLine}`;
  }
}

function chartConfig(m, checks, big) {
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
    { type: "line", label: "TTFT", data: line, borderColor: "#3b82f6", backgroundColor: "#3b82f6", pointRadius: big ? 3 : 2, borderWidth: 1.5, tension: 0.2 },
    { type: "scatter", label: "TTFT 异常", data: anomalyPts, backgroundColor: "#f59e0b", pointRadius: big ? 5 : 3 },
    { type: "scatter", label: "失败", data: failPts, backgroundColor: "#ef4444", pointStyle: "crossRot", borderColor: "#ef4444", pointRadius: big ? 6 : 4, borderWidth: 2 },
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
        x: { type: "linear", min: xMin, max: xMax, ticks: { callback: (v) => fmtShort(v), maxTicksLimit: big ? 8 : 4, font: { size: 10 } } },
        y: { beginAtZero: true, ticks: { callback: (v) => fmtMs(v), font: { size: 10 } } },
      },
      plugins: {
        legend: { display: big, labels: { boxWidth: 10 } },
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

async function loadCardChart(m, hours) {
  const card = $(`.model-card[data-id="${m.id}"]`);
  if (!card) return;
  let checks;
  try {
    checks = await api(`/api/models/${m.id}/checks?hours=${hours}`);
  } catch {
    return;
  }
  const cfg = chartConfig(m, checks, false);
  const old = state.charts.get(m.id);
  if (old) {
    old.data = cfg.data;
    old.options = cfg.options;
    old.update("none");
  } else {
    state.charts.set(m.id, new Chart($("canvas", card), cfg));
  }
}

async function onCardAction(e, id) {
  const act = e.target.closest("button")?.dataset.act;
  if (!act) return;
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
      loadDashboard();
    }
  } else if (act === "detail") {
    state.detailId = id;
    $("#detail-dialog").showModal();
    refreshDetail();
  }
}
$("#range-select").addEventListener("change", loadDashboard);

// ---------- detail ----------
let detailChart = null;
async function refreshDetail() {
  const m = state.models.find((x) => x.id === state.detailId);
  if (!m) return;
  const hours = Number($("#range-select").value);
  $("#detail-title").textContent = m.name;
  $("#detail-meta").innerHTML = `${KIND[m.kind]} · 部署 ${esc(m.deployment)} · Key ${esc(m.api_key_masked)}<br>请求地址：<code>${esc(m.request_url)}</code>${m.kind === "text" ? ` · token 参数：<code>${esc(m.token_param)}</code>` : ""}`;
  const checks = await api(`/api/models/${m.id}/checks?hours=${hours}`).catch(() => []);
  const cfg = chartConfig(m, checks, true);
  if (detailChart) detailChart.destroy();
  detailChart = new Chart($("#detail-chart"), cfg);
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
  const form = $("#model-form");
  form.reset();
  $("#model-form-error").textContent = "";
  state.editingId = m ? m.id : null;
  $("#model-dialog-title").textContent = m ? "编辑模型" : "添加模型";
  form.api_key.placeholder = m ? `已保存 ${m.api_key_masked}，留空保持不变` : "";
  form.api_key.required = !m;
  if (m) {
    for (const k of ["name", "kind", "endpoint", "deployment", "interval_minutes"]) form[k].value = m[k];
    form.ttft_threshold_pct.value = m.ttft_threshold_pct ?? "";
    form.baseline_manual_ms.value = m.baseline_manual_ms ?? "";
    form.enabled.checked = m.enabled;
  } else {
    const s = await api("/api/settings").catch(() => ({}));
    form.interval_minutes.value = s.default_interval_minutes || 30;
    form.enabled.checked = true;
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
    endpoint: f.endpoint.value,
    deployment: f.deployment.value,
    api_key: f.api_key.value || null,
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
