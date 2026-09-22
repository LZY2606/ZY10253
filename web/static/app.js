const state = { runId: null, detail: null, runs: [], lastEventKey: null };

const $ = (id) => document.getElementById(id);
async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || resp.statusText);
  return data;
}
function esc(value) {
  return String(value ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function fracText(amount) {
  if (!amount) return "0";
  if (amount.includes("/")) {
    const [n, d] = amount.split("/").map(Number);
    if ([2, 4, 5, 8, 10, 16, 20, 25, 40, 50, 100, 125, 200, 250, 500, 1000].includes(d)) {
      const dec = n / d;
      return Number(dec.toFixed(4)).toString();
    }
    return amount;
  }
  return amount;
}
function clockText(exact) {
  return `${fracText(exact)} s`;
}

async function loadRuns(selectId = null) {
  const data = await api("/api/runs");
  state.runs = data.runs;
  const select = $("runSelect");
  select.innerHTML = "";
  for (const run of data.runs) {
    const option = document.createElement("option");
    option.value = run.id;
    option.textContent = `#${run.id} ${run.label} [${run.status}]`;
    select.appendChild(option);
  }
  if (!state.runId && data.runs.length) state.runId = data.runs[data.runs.length - 1].id;
  if (state.runId) select.value = state.runId;
  renderRunTree();
}

function renderRunTree() {
  const byRoot = new Map();
  for (const run of state.runs) {
    if (!byRoot.has(run.root_id)) byRoot.set(run.root_id, []);
    byRoot.get(run.root_id).push(run);
  }
  $("runTree").innerHTML = [...byRoot.values()].map((runs) => {
    runs.sort((a, b) => a.id - b.id);
    return runs.map((run) => `
      <div class="runline ${run.id === state.runId ? "active" : ""}" data-run="${run.id}">
        <span>${run.parent_id ? "└─ 恢复分支 " : ""}#${run.id} ${esc(run.label)}</span>
        <span class="muted">${run.status}</span>
      </div>`).join("");
  }).join("");
  document.querySelectorAll(".runline").forEach((el) =>
    el.addEventListener("click", () => { state.runId = Number(el.dataset.run); refresh(); }));
}

function renderDeck(detail) {
  const s = detail.state;
  const head = s.head_slot;
  const slots = ["HOM", "SRC", "TIP", "PLT", "WST", "BIN"];
  const html = slots.map((slot) => {
    const container = s.containers[slot];
    let body = "";
    if (container) {
      if (container.wells.length) {
        body = container.wells.map((w) => `
          <div class="well"><span>${esc(w.name)}</span>
            <span class="vol">${fracText(w.total)}/${fracText(w.capacity)} µL</span>
          </div>`).join("");
      } else {
        body = `<div class="muted">机械臂停靠点</div>`;
      }
    }
    if (slot === "TIP") {
      const rack = s.tips.TIP || [];
      body += `<div class="tips" style="margin-top:6px">${rack.map((t) =>
        `<span class="tip ${t.state}">${esc(t.position)}${t.held_total !== "0" ? "·" + fracText(t.held_total) : ""}</span>`).join("")}</div>`;
    }
    return `<div class="slot ${head === slot ? "active" : ""}">
      <div class="slot-name">${slot}${head === slot ? " · 机械臂在此" : ""}</div>
      <div class="slot-label">${esc(container ? container.label : "")}</div>${body}
    </div>`;
  }).join("");
  $("deck").innerHTML = html;
}

function opName(name) {
  return { move_head: "模块移动", pick_tip: "拾取吸头", aspirate: "吸液",
    dispense: "排液", eject_tip: "退出吸头", resume_branch: "恢复分叉" }[name] || name;
}

function renderTimeline(detail) {
  const events = detail.events;
  if (!events.length) {
    $("timeline").innerHTML = `<div class="event"><span class="muted">尚无事件。点击“单步执行”开始重放。</span></div>`;
    $("summary").innerHTML = "";
    return;
  }
  const last = events[events.length - 1];
  state.lastEventKey = last.replay_key;
  $("timeline").innerHTML = events.map((ev) => {
    const tagClass = ev.op_name === "resume_branch" ? "resume" : ev.ok ? "ok" : "fail";
    const tagText = ev.op_name === "resume_branch" ? "恢复" : ev.ok ? "成功" : "失败";
    const detailText = ev.fault ? `故障: ${ev.fault}` :
      (ev.op_name === "aspirate" || ev.op_name === "dispense")
        ? `${(ev.payload.well || "")} ${fracText(ev.payload.volume || "0")} µL`
        : ev.op_name === "move_head" ? `→ ${ev.payload.target || ""}`
        : ev.op_name === "resume_branch" ? "从检查点恢复"
        : `${ev.payload.tip || ""}`;
    return `<div class="event ${ev.ok ? "" : "fail"} ${ev.payload && ev.payload.compound_commit ? "commit" : ""}">
      <span class="seq">${ev.seq}</span>
      <span>${esc(ev.step_id || "—")}<br><span class="muted">${opName(ev.op_name)}</span></span>
      <span>${esc(detailText)}${ev.error ? `<br><span class="tag fail">${esc(ev.error)}</span>` : ""}</span>
      <span class="tag ${tagClass}">${tagText}<br><span class="muted">+${fracText(ev.delta_clock)}s</span></span>
    </div>`;
  }).join("");
  $("summary").innerHTML = `<b>before</b> 时钟 ${clockText(last.before.clock)}，
    机械臂 ${esc(last.before.head ? last.before.head.head_slot : "")}<br>
    <b>after</b> 时钟 ${clockText(last.after.clock)}，
    持液吸头 ${esc(last.after.head && last.after.head.picked_tip ?
      last.after.head.picked_tip.tip + " (" + fracText(last.after.head.picked_tip.held_total) + " µL)" : "无")}`;
}

function renderLineage(detail) {
  const lineage = detail.lineage;
  const held = detail.held_tips || [];
  const heldHtml = held.length
    ? held.map((t) => `<span class="pill held">${esc(t.tip)} 持液 ${fracText(t.held_total)} µL</span>`).join("")
    : "";
  const rows = Object.entries(lineage.balances).map(([ref, comps]) => {
    const compHtml = Object.entries(comps)
      .map(([c, v]) => `<span class="pill">${esc(c)}: ${fracText(v)} µL</span>`).join("");
    return `<div class="lrow"><b>${esc(ref)}</b><br>${compHtml || '<span class="muted">空</span>'}</div>`;
  }).join("");
  $("lineage").innerHTML = `
    <div class="muted">谱系与物理状态对账：
      <b style="color:${lineage.reconciles_with_physical ? "var(--ok)" : "var(--bad)"}">
      ${lineage.reconciles_with_physical ? "一致" : "不一致"}</b></div>
    ${heldHtml ? `<div style="margin:6px 0">${heldHtml}</div>` : ""}
    ${rows}`;
}

function renderConservation(detail) {
  const c = detail.conservation;
  $("conservation").innerHTML = `
    <div>精确总量 before：<b>${fracText(c.exact_before)} µL</b></div>
    <div>精确总量 after：<b>${fracText(c.exact_after)} µL</b></div>
    <div>精确差额：<b style="color:${c.conserved ? "var(--ok)" : "var(--bad)"}">
      ${fracText(c.exact_delta)} µL</b></div>
    <div class="muted">NumPy 浮点合计（仅展示）：${c.numpy_after_total.toFixed(6)} µL</div>
    <div class="muted">守恒：${c.conserved ? "成立（Fraction）" : "被破坏"}</div>`;
}

function render(detail) {
  const run = detail.run;
  $("runId").textContent = run.id;
  $("clock").textContent = clockText(detail.state.clock);
  const statusEl = $("runStatus");
  statusEl.textContent = { ready: "可执行", done: "已完成", failed: "待恢复", recovered: "已恢复分叉" }[run.status] || run.status;
  statusEl.className = `status ${run.status}`;
  renderDeck(detail);
  renderTimeline(detail);
  renderLineage(detail);
  renderConservation(detail);
  const failed = run.status === "failed";
  $("stepBtn").disabled = failed || run.status === "done" || run.status === "recovered";
  $("injectBtn").disabled = !$("stepBtn").disabled === false && !failed;
  $("injectBtn").disabled = run.status !== "ready";
  $("resumeBtn").style.display = failed ? "" : "none";
  $("replayBtn").disabled = !state.lastEventKey;
  $("notice").textContent = failed
    ? (detail.recovery_pending
      ? "复合转移中途失败：现实吸头仍持有液体，运行进入待恢复；恢复将从最后可提交检查点分叉，不覆盖本运行。"
      : "运行失败，可从最后可提交检查点恢复。")
    : "";
}

async function refresh() {
  await loadRuns();
  if (state.runId) {
    const detail = await api(`/api/runs/${state.runId}`);
    state.detail = detail;
    render(detail);
  } else {
    $("runId").textContent = "—";
    $("clock").textContent = "0 s";
  }
}

$("newRun").addEventListener("click", async () => {
  const run = await api("/api/runs", { method: "POST", body: JSON.stringify({}) });
  state.runId = run.id;
  refresh();
});
$("runSelect").addEventListener("change", (e) => { state.runId = Number(e.target.value); refresh(); });
$("stepBtn").addEventListener("click", async () => {
  await api(`/api/runs/${state.runId}/step`, { method: "POST", body: "{}" });
  refresh();
});
$("replayBtn").addEventListener("click", async () => {
  await api(`/api/runs/${state.runId}/step`, {
    method: "POST",
    body: JSON.stringify({ replay_key: state.lastEventKey }),
  });
  refresh();
});
$("injectBtn").addEventListener("click", async () => {
  try {
    await api(`/api/runs/${state.runId}/inject`, {
      method: "POST",
      body: JSON.stringify({ fault: $("faultType").value, note: "页面手动注入" }),
    });
    await api(`/api/runs/${state.runId}/step`, { method: "POST", body: "{}" });
    refresh();
  } catch (e) { alert(e.message); }
});
$("resumeBtn").addEventListener("click", async () => {
  const child = await api(`/api/runs/${state.runId}/resume`, { method: "POST", body: "{}" });
  state.runId = child.id;
  refresh();
});
$("exportBtn").addEventListener("click", async () => {
  const bundle = await api("/api/export");
  $("ioBox").value = JSON.stringify(bundle, null, 2);
});
$("copyBtn").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("ioBox").value || JSON.stringify(await api("/api/export")));
});
$("resetBtn").addEventListener("click", async () => {
  if (!confirm("确认清空 SQLite 全部运行记录？")) return;
  await api("/api/reset", { method: "POST" });
  state.runId = null;
  $("ioBox").value = "";
  refresh();
});
$("importBtn").addEventListener("click", async () => {
  try {
    const bundle = JSON.parse($("ioBox").value);
    const result = await api("/api/import", { method: "POST", body: JSON.stringify({ bundle }) });
    alert(`导入完成：${result.imported_runs} 个运行；重放核对 ${result.verification.verified ? "通过" : "失败"}`);
    refresh();
  } catch (e) { alert("导入失败：" + e.message); }
});

refresh().catch((e) => { $("notice").textContent = e.message; });
