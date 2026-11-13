/* 迟到更正处理台前端 */
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (v) => (v === null || v === undefined) ? "—" :
  (typeof v === "number" ? (Number.isInteger(v) ? v : v.toFixed(2)) : esc(v));

async function api(path, method = "GET", body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(msg, isErr = false) {
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = msg;
  $("#toast").appendChild(el);
  setTimeout(() => el.remove(), 4200);
}
const ok = (m) => toast(m);
const err = (e) => toast(e.message || String(e), true);

const STATUS_CN = {
  pending: "待确认", active: "已生效", withdrawn: "已撤回",
  manual_pending: "人工队列", manual_closed: "已线下关闭",
  preview: "预览中", confirmed: "已确认", cancelled: "已取消",
  draft: "草稿", published: "已发布", superseded: "被取代",
};
const TRIGGER_CN = {
  snapshot: "快照导入", correction: "更正到达",
  withdrawal: "更正撤回", manual_force: "人工强制回补",
};
const badge = (t, cls = "") => `<span class="badge ${cls}">${esc(t)}</span>`;
const statusBadge = (s) => {
  const cls = { active: "ok", confirmed: "ok", published: "ok", pending: "warn",
    preview: "warn", manual_pending: "warn", withdrawn: "muted",
    cancelled: "muted", superseded: "muted", manual_closed: "muted",
    draft: "" }[s] || "";
  return badge(STATUS_CN[s] || s, cls);
};

/* ------------------------------------------------------------ 总览 */
async function renderOverview(el) {
  const o = await api("/api/overview");
  el.innerHTML = `
    <div class="cards">
      ${card("快照日期数", o.snapshot_days)}${card("源记录数", o.source_records)}
      ${card("生效更正", o.corrections.active)}${card("待确认更正", o.corrections.pending, o.corrections.pending > 0)}
      ${card("已撤回", o.corrections.withdrawn)}${card("预览中重算", o.preview_runs, o.preview_runs > 0)}
      ${card("已发布报表", o.published_reports)}${card("过期报表", o.stale_reports, o.stale_reports > 0)}
      ${card("人工队列待办", o.manual_pending, o.manual_pending > 0)}
    </div>
    <div class="panel"><h2>运行参数</h2>
      <p>当前规则版本：${badge(o.current_rule)}　可回补窗口：${o.backfill_window_days} 天
      　<span class="muted">超出窗口的更正进入人工队列</span></p>
    </div>
    <div class="panel"><h2>最近动态</h2>${auditTable(o.recent_audit)}</div>`;
}
const card = (label, num, alert = false) =>
  `<div class="card ${alert ? "alert" : ""}"><div class="label">${label}</div><div class="num">${num}</div></div>`;
const auditTable = (rows) => `<table><tr><th>时间</th><th>动作</th><th>详情</th></tr>
  ${rows.map((a) => `<tr><td class="mono">${esc(a.at)}</td><td>${esc(a.action)}</td>
    <td class="mono">${esc(a.detail || "")}</td></tr>`).join("")}</table>`;

/* ------------------------------------------------------------ 数据导入 */
const SNAP_EXAMPLE = JSON.stringify({
  biz_date: "2026-11-10",
  records: [
    { order_id: "O1110-E1", channel: "电商", amount: 320, qty: 2 },
    { order_id: "O1110-S1", channel: "门店", amount: 180, qty: 1,
      status: "refunded", refund_amount: 50 },
  ],
}, null, 2);
const CORR_EXAMPLE = JSON.stringify({
  corrections: [
    { correction_id: "C-9001", type: "adjust", target_order_id: "O1110-E1",
      amount: 360, reason: "金额更正" },
    { correction_id: "C-9002", type: "void", target_order_id: "O1110-S1" },
    { correction_id: "C-9003", type: "insert", biz_date: "2026-11-09",
      order_id: "O1109-E9", channel: "电商", amount: 120, qty: 1 },
  ],
}, null, 2);

async function renderImport(el) {
  el.innerHTML = `
  <div class="grid2">
    <div class="panel"><h2>导入每日业务快照</h2>
      <p class="muted">同一日期重复导入会生成新快照版本并进入重算预览，不会直接覆盖已确认结果。</p>
      <textarea id="snap-json">${esc(SNAP_EXAMPLE)}</textarea>
      <div class="section-gap"><button class="btn primary" id="imp-snap">导入快照</button></div>
    </div>
    <div class="panel"><h2>导入更正记录</h2>
      <p class="muted">type: adjust(改金额/数量/状态) | void(作废) | insert(漏单补录)。
      同一 correction_id 重复到达不会重复叠加；超出回补窗口进人工队列。</p>
      <textarea id="corr-json">${esc(CORR_EXAMPLE)}</textarea>
      <div class="section-gap"><button class="btn primary" id="imp-corr">导入更正</button></div>
    </div>
  </div>
  <div class="panel"><h2>导入结果</h2><pre id="imp-result">（尚未导入）</pre></div>`;
  $("#imp-snap").onclick = async () => {
    try {
      const r = await api("/api/import/snapshot", "POST", JSON.parse($("#snap-json").value));
      $("#imp-result").textContent = JSON.stringify(r, null, 2);
      ok("快照导入完成"); refreshBadge();
    } catch (e) { err(e); }
  };
  $("#imp-corr").onclick = async () => {
    try {
      const r = await api("/api/import/corrections", "POST", JSON.parse($("#corr-json").value));
      $("#imp-result").textContent = JSON.stringify(r, null, 2);
      ok(r.run_id ? `已生成重算预览 ${r.run_id}，请到「重算中心」确认` : "更正已处理");
      refreshBadge();
    } catch (e) { err(e); }
  };
}

/* ------------------------------------------------------------ 更正记录 */
async function renderCorrections(el) {
  const { corrections } = await api("/api/corrections");
  el.innerHTML = `<div class="panel"><h2>更正记录（重复到达只计数、不叠加）</h2>
    <table><tr><th>ID</th><th>类型</th><th>目标单号</th><th>业务日期</th><th>状态</th>
      <th>到达次数</th><th>内容</th><th>接收时间</th><th>操作</th></tr>
    ${corrections.map((c) => `<tr>
      <td class="mono">${esc(c.correction_id)}</td><td>${esc(c.type)}</td>
      <td class="mono">${esc(c.target_order_id || "—")}</td><td>${esc(c.biz_date || "—")}</td>
      <td>${statusBadge(c.status)}</td>
      <td>${c.arrival_count > 1 ? badge(`×${c.arrival_count}`, "warn") : 1}</td>
      <td class="mono">${esc(c.payload)}</td><td class="mono">${esc(c.received_at)}</td>
      <td>${c.status === "active" || c.status === "pending"
        ? `<button class="btn small danger" data-w="${esc(c.correction_id)}">撤回</button>` : ""}</td>
    </tr>`).join("")}</table></div>`;
  el.querySelectorAll("[data-w]").forEach((b) => b.onclick = async () => {
    if (!confirm(`确认撤回更正 ${b.dataset.w}？将触发恢复重算（预览后确认）。`)) return;
    try {
      const r = await api("/api/corrections/withdraw", "POST", { correction_id: b.dataset.w });
      ok(r.run_id ? `撤回成功，恢复重算 ${r.run_id} 待确认` : "撤回成功（未生效的更正已直接撤回）");
      renderCorrections(el); refreshBadge();
    } catch (e) { err(e); }
  });
}

/* ------------------------------------------------------------ 重算中心 */
async function renderRuns(el, runId) {
  const { runs } = await api("/api/runs");
  let html = `<div class="panel"><h2>重算运行</h2>
    <table><tr><th>运行</th><th>触发</th><th>触发对象</th><th>规则版本</th><th>影响日期</th>
      <th>下游报表</th><th>状态</th><th>创建时间</th></tr>
    ${runs.map((r) => `<tr class="${r.run_id === runId ? "changed" : ""}">
      <td class="mono clickable" data-run="${r.run_id}">${r.run_id}</td>
      <td>${TRIGGER_CN[r.trigger_type] || r.trigger_type}</td>
      <td class="mono">${esc((r.trigger_refs.correction_ids || [r.trigger_refs.snapshot_id || ""]).join(", "))}</td>
      <td>${badge(r.rule_version)}</td>
      <td class="mono">${esc(r.affected_dates.join(", "))}</td>
      <td class="mono">${esc(r.affected_reports.join(", "))}</td>
      <td>${statusBadge(r.status)}</td><td class="mono">${esc(r.created_at)}</td></tr>`).join("")}
    </table></div><div id="run-detail"></div>`;
  el.innerHTML = html;
  el.querySelectorAll("[data-run]").forEach((t) =>
    t.onclick = () => renderRuns(el, t.dataset.run));
  if (runId) {
    const r = await api(`/api/runs/${runId}`);
    const canAct = r.status === "preview";
    $("#run-detail").innerHTML = `<div class="panel">
      <h2>${r.run_id} 重算预览 ${statusBadge(r.status)}</h2>
      <p class="muted">触发：${TRIGGER_CN[r.trigger_type]} ·
        ${esc((r.trigger_refs.correction_ids || []).join(", ") || r.trigger_refs.snapshot_id || "")}
        　使用规则版本：${badge(r.rule_version)}　影响下游报表：
        ${esc(r.affected_reports.join(", ") || "无")}</p>
      <table><tr><th>日期</th><th>指标</th><th>维度</th><th>旧值</th><th>旧版本</th>
        <th>新值</th><th>差异</th><th>该单元格吃到的更正</th></tr>
      ${r.items.map((it) => {
        const d = it.old_value === null ? null : +(it.new_value - it.old_value).toFixed(2);
        return `<tr class="${d ? "changed" : ""}">
        <td class="mono">${it.biz_date}</td><td>${it.metric}</td><td>${it.dim}</td>
        <td class="old">${fmt(it.old_value)}${it.old_rule_version
          ? ` <span class="muted">(${it.old_rule_version}@v${it.old_version_no})</span>` : ""}</td>
        <td class="mono">${it.old_version_no ?? "—"}</td>
        <td><b>${fmt(it.new_value)}</b></td>
        <td>${d === null ? badge("新增", "warn") : d === 0 ? "—"
          : `<span class="${d > 0 ? "diff-pos" : "diff-neg"}">${d > 0 ? "+" : ""}${d}</span>`}</td>
        <td class="mono">${esc(it.corrections_applied.join(", ") || "—")}</td></tr>`;
      }).join("")}</table>
      ${canAct ? `<div class="section-gap">
        <button class="btn primary" id="cf-run">确认并生成新版本</button>
        ${r.trigger_type !== "withdrawal" ? `<button class="btn" id="cc-run">取消（更正转人工队列）</button>` : ""}
      </div>` : ""}</div>`;
    if (canAct) {
      $("#cf-run").onclick = async () => {
        try { await api("/api/runs/confirm", "POST", { run_id: r.run_id });
          ok(`${r.run_id} 已确认，新结果版本已创建`); renderRuns(el, r.run_id); refreshBadge();
        } catch (e) { err(e); }
      };
      const cc = $("#cc-run");
      if (cc) cc.onclick = async () => {
        try { await api("/api/runs/cancel", "POST", { run_id: r.run_id });
          ok(`${r.run_id} 已取消`); renderRuns(el); refreshBadge();
        } catch (e) { err(e); }
      };
    }
  }
}

/* ------------------------------------------------------------ 指标版本 */
async function renderCells(el, sel) {
  const { cells } = await api("/api/cells");
  el.innerHTML = `<div class="panel"><h2>指标单元格（当前版本）</h2>
    <table><tr><th>日期</th><th>指标</th><th>维度</th><th>当前值</th><th>版本</th>
      <th>规则版本</th><th>应用的更正</th><th>来源运行</th></tr>
    ${cells.map((c) => `<tr class="clickable-row" data-cell='${esc(JSON.stringify(
      { d: c.biz_date, m: c.metric, dim: c.dim }))}'>
      <td class="mono">${c.biz_date}</td><td>${c.metric}</td><td>${c.dim}</td>
      <td><b>${fmt(c.value)}</b></td><td class="mono">v${c.version_no}</td>
      <td>${badge(c.rule_version)}</td>
      <td class="mono">${esc(JSON.parse(c.corrections_applied).join(", ") || "—")}</td>
      <td class="mono">${esc(c.run_id || "—")}</td></tr>`).join("")}
    </table><p class="muted">点击行查看该单元格的完整版本历史。</p></div>
    <div id="cell-hist"></div>`;
  el.querySelectorAll("[data-cell]").forEach((t) => t.onclick = async () => {
    const { d, m, dim } = JSON.parse(t.dataset.cell);
    const { versions } = await api(
      `/api/cell-versions?date=${d}&metric=${m}&dim=${encodeURIComponent(dim)}`);
    $("#cell-hist").innerHTML = `<div class="panel">
      <h2>版本历史：${d} · ${m} · ${esc(dim)}</h2>
      <table><tr><th>版本</th><th>值</th><th>规则版本</th><th>运行</th><th>应用的更正</th>
        <th>恢复自</th><th>创建时间</th></tr>
      ${versions.map((v) => `<tr>
        <td class="mono">v${v.version_no}</td><td><b>${fmt(v.value)}</b></td>
        <td>${badge(v.rule_version)}</td><td class="mono">${esc(v.run_id || "—")}</td>
        <td class="mono">${esc(JSON.parse(v.corrections_applied).join(", ") || "—")}</td>
        <td class="mono">${v.restored_from_version_id
          ? `版本#${v.restored_from_version_id}（撤回恢复）` : "—"}</td>
        <td class="mono">${esc(v.created_at)}</td></tr>`).join("")}</table></div>`;
  });
  if (sel) {} // 预留深链
}

/* ------------------------------------------------------------ 报表中心 */
async function renderReports(el) {
  const { reports } = await api("/api/reports");
  el.innerHTML = `<div class="panel"><h2>下游报表
      <button class="btn small" id="gen-all" style="margin-left:10px">按当前指标重新生成全部草稿</button></h2>
    ${reports.map((d) => `<h3 class="mono">${esc(d.report_key)}
        <span class="muted">（${d.date_from} ~ ${d.date_to}）</span></h3>
      <table><tr><th>版本</th><th>状态</th><th>规则版本</th><th>来源运行</th>
        <th>创建时间</th><th>操作</th></tr>
      ${d.versions.map((v) => `<tr>
        <td class="mono">v${v.version_no}</td>
        <td>${statusBadge(v.status)}${v.stale && v.status === "published" ? " " + badge("已过期", "warn") : ""}</td>
        <td>${badge(v.rule_version)}</td><td class="mono">${esc(v.run_id || "手工")}</td>
        <td class="mono">${esc(v.created_at)}</td>
        <td>${v.status === "draft"
          ? `<button class="btn small primary" data-pub="${v.id}"
              ${v.blockers.length ? "disabled" : ""}>发布</button>
             ${v.blockers.length ? `<span class="badge bad">${esc(v.blockers[0])}</span>` : ""}`
          : ""}
          <button class="btn small" data-view="${v.id}">明细</button></td>
      </tr>`).join("")}</table>`).join("")}</div>
    <div id="report-detail"></div>`;
  $("#gen-all").onclick = async () => {
    try { const r = await api("/api/reports/generate", "POST", {});
      ok(`已生成 ${r.generated.length} 个报表草稿`); renderReports(el);
    } catch (e) { err(e); }
  };
  el.querySelectorAll("[data-pub]").forEach((b) => b.onclick = async () => {
    try {
      const r = await api("/api/reports/publish", "POST", { report_version_id: +b.dataset.pub });
      if (r.ok) { ok("发布成功"); } else { toast(r.blockers.join("；"), true); }
      renderReports(el); refreshBadge();
    } catch (e) { err(e); }
  });
  el.querySelectorAll("[data-view]").forEach((b) => b.onclick = async () => {
    const { version, items } = await api(`/api/report-versions/${b.dataset.view}`);
    $("#report-detail").innerHTML = `<div class="panel">
      <h2>${esc(version.report_key)} v${version.version_no} 明细 ${statusBadge(version.status)}</h2>
      <table><tr><th>指标</th><th>维度</th><th>值</th><th>血缘（指标版本数）</th></tr>
      ${items.map((it) => `<tr><td>${it.metric}</td><td>${it.dim}</td>
        <td><b>${fmt(it.value)}</b></td>
        <td class="mono">${JSON.parse(it.cell_version_ids).length} 个单元格版本</td></tr>`).join("")}
      </table></div>`;
  });
}

/* ------------------------------------------------------------ 版本对比 + 溯源 */
async function renderCompare(el) {
  const { reports } = await api("/api/reports");
  const opts = reports.flatMap((d) => d.versions.map((v) =>
    `<option value="${v.id}">${esc(d.report_key)} v${v.version_no}（${STATUS_CN[v.status]}${v.stale ? "/过期" : ""}）</option>`));
  el.innerHTML = `<div class="panel"><h2>报表版本对比</h2>
    <p>版本 A <select id="sel-a">${opts}</select>
       版本 B <select id="sel-b">${opts}</select>
       <button class="btn primary" id="do-cmp">对比</button></p>
    <div id="cmp-result"></div></div><div id="trace-result"></div>`;
  const selB = $("#sel-b");
  if (selB.options.length > 1) selB.selectedIndex = 1;
  $("#do-cmp").onclick = async () => {
    try {
      const r = await api(`/api/compare?a=${$("#sel-a").value}&b=${$("#sel-b").value}`);
      $("#cmp-result").innerHTML = `
        <h3>A = ${esc(r.a.report_key)} v${r.a.version_no}（${r.a.rule_version}）
          　B = ${esc(r.b.report_key)} v${r.b.version_no}（${r.b.rule_version}）</h3>
        <table><tr><th>指标</th><th>维度</th><th>A 值</th><th>B 值</th><th>差异(B-A)</th><th>溯源</th></tr>
        ${r.rows.map((w) => `<tr class="${w.changed ? "changed" : ""}">
          <td>${w.metric}</td><td>${w.dim}</td>
          <td class="old">${fmt(w.a)}</td><td><b>${fmt(w.b)}</b></td>
          <td>${w.delta === null ? "—" : w.delta === 0 ? "0"
            : `<span class="${w.delta > 0 ? "diff-pos" : "diff-neg"}">${w.delta > 0 ? "+" : ""}${w.delta}</span>`}</td>
          <td><button class="btn small" data-trace='${esc(JSON.stringify(
            { rv: r.b.id, m: w.metric, dim: w.dim }))}'>追到源记录</button></td>
        </tr>`).join("")}</table>`;
      $("#cmp-result").querySelectorAll("[data-trace]").forEach((b) => b.onclick = async () => {
        const { rv, m, dim } = JSON.parse(b.dataset.trace);
        await renderTrace(rv, m, dim);
      });
    } catch (e) { err(e); }
  };
}

async function renderTrace(rvid, metric, dim) {
  try {
    const t = await api(`/api/trace?report_version_id=${rvid}&metric=${metric}&dim=${encodeURIComponent(dim)}`);
    $("#trace-result").innerHTML = `<div class="panel">
      <h2>血缘追溯：${t.metric} · ${esc(t.dim)} = <b>${fmt(t.value)}</b></h2>
      ${t.cells.map((c) => `<div class="lineage">
        <div>指标版本 <span class="mono">#${c.cell_version_id}</span>（${c.biz_date} v${c.version_no}，
          规则 ${badge(c.rule_version)}，值 <b>${fmt(c.value)}</b>，来自 ${esc(c.run_id || "—")}）
          ${c.restored_from_version_id ? ` ${badge(`撤回恢复自 #${c.restored_from_version_id}`, "warn")}` : ""}</div>
        ${c.corrections.length ? `<div>触发更正：${c.corrections.map((x) =>
          `${badge(x.correction_id, x.status === "withdrawn" ? "muted" : "warn")}
           <span class="mono">${esc(x.type)} ${esc(JSON.stringify(x.payload))}</span>`).join(" ")}</div>` : ""}
        ${c.records.length ? `<table><tr><th>单号</th><th>渠道</th><th>金额</th><th>数量</th>
          <th>状态</th><th>退款</th><th>快照</th></tr>
          ${c.records.map((r) => `<tr><td class="mono">${esc(r.order_id)}</td><td>${esc(r.channel)}</td>
            <td>${fmt(r.amount)}</td><td>${r.qty}</td><td>${esc(r.status)}</td>
            <td>${fmt(r.refund_amount)}</td><td class="mono">${esc(r.snapshot_id)}</td></tr>`).join("")}
          </table>` : "<div class='muted'>该单元格无快照源记录（值完全来自补录更正）。</div>"}
      </div>`).join("")}</div>`;
    $("#trace-result").scrollIntoView({ behavior: "smooth" });
  } catch (e) { err(e); }
}

/* ------------------------------------------------------------ 人工队列 */
async function renderManual(el) {
  const { queue } = await api("/api/manual-queue");
  el.innerHTML = `<div class="panel"><h2>人工处理队列</h2>
    <p class="muted">超出可回补窗口、目标缺失或 ID 冲突的更正在此等待人工裁决。</p>
    <table><tr><th>#</th><th>更正</th><th>原因</th><th>状态</th><th>业务日期</th>
      <th>内容</th><th>处理结果</th><th>操作</th></tr>
    ${queue.map((q) => `<tr>
      <td class="mono">${q.id}</td><td class="mono">${esc(q.correction_id)}</td>
      <td>${badge(q.reason, "warn")}</td>
      <td>${q.status === "pending" ? badge("待处理", "warn") : badge("已处理", "ok")}</td>
      <td class="mono">${esc(q.correction?.biz_date || "—")}</td>
      <td class="mono">${esc(q.correction ? JSON.stringify(q.correction.payload) : "")}</td>
      <td class="mono">${esc(q.resolution || "—")}</td>
      <td>${q.status === "pending" ? `
        <button class="btn small primary" data-force="${q.id}">强制回补</button>
        <button class="btn small" data-close="${q.id}">线下关闭</button>` : ""}</td>
    </tr>`).join("")}</table></div>`;
  el.querySelectorAll("[data-force]").forEach((b) => b.onclick = async () => {
    if (!confirm("强制回补将忽略窗口限制并生成重算预览，确认？")) return;
    try {
      const r = await api("/api/manual-queue/resolve", "POST", { id: +b.dataset.force, action: "force" });
      ok(r.run_id ? `已生成重算预览 ${r.run_id}` : "已强制回补（无值变化）");
      renderManual(el); refreshBadge();
    } catch (e) { err(e); }
  });
  el.querySelectorAll("[data-close]").forEach((b) => b.onclick = async () => {
    const note = prompt("关闭备注（线下处理方式）：") || "";
    try {
      await api("/api/manual-queue/resolve", "POST", { id: +b.dataset.close, action: "close", note });
      ok("已关闭"); renderManual(el); refreshBadge();
    } catch (e) { err(e); }
  });
}

/* ------------------------------------------------------------ 审计 */
async function renderAudit(el) {
  const { audit } = await api("/api/audit");
  el.innerHTML = `<div class="panel"><h2>审计日志</h2>${auditTable(audit)}</div>`;
}

/* ------------------------------------------------------------ 框架 */
const TABS = [
  ["overview", "总览", renderOverview],
  ["import", "数据导入", renderImport],
  ["corrections", "更正记录", renderCorrections],
  ["runs", "重算中心", (el) => renderRuns(el)],
  ["cells", "指标版本", (el) => renderCells(el)],
  ["reports", "报表中心", renderReports],
  ["compare", "版本对比", renderCompare],
  ["manual", "人工队列", renderManual],
  ["audit", "审计日志", renderAudit],
];
let currentTab = "overview";

async function refreshBadge() {
  try {
    const o = await api("/api/overview");
    $("#rule-badge").textContent = `规则 ${o.current_rule} · 回补窗口 ${o.backfill_window_days}天`;
  } catch (e) { /* 忽略 */ }
}

async function switchTab(name) {
  currentTab = name;
  document.querySelectorAll("nav button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name));
  const el = $("#main");
  el.innerHTML = "<p class='muted'>加载中…</p>";
  const fn = TABS.find((t) => t[0] === name)[2];
  try { await fn(el); } catch (e) { el.innerHTML = `<div class="panel">加载失败：${esc(e.message)}</div>`; }
}

window.addEventListener("DOMContentLoaded", () => {
  const nav = $("#tabs");
  TABS.forEach(([name, label]) => {
    const b = document.createElement("button");
    b.textContent = label; b.dataset.tab = name;
    b.onclick = () => switchTab(name);
    nav.appendChild(b);
  });
  $("#btn-seed").onclick = async () => {
    if (!confirm("加载演示数据？（不会清空现有数据，建议先「清空重置」）")) return;
    try { const r = await api("/api/demo/seed", "POST", {});
      ok(`演示数据已加载：${r.days} 天快照（${r.range[0]} ~ ${r.range[1]}）`);
      switchTab(currentTab); refreshBadge();
    } catch (e) { err(e); }
  };
  $("#btn-reset").onclick = async () => {
    if (!confirm("确认清空全部业务数据？")) return;
    try { await api("/api/reset", "POST", {}); ok("已清空"); switchTab(currentTab); refreshBadge();
    } catch (e) { err(e); }
  };
  refreshBadge();
  switchTab("overview");
});
