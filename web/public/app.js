/* HermesQA demo site — đọc Firestore theo thời gian thực, đăng nhập GitHub qua Firebase Auth,
   gọi API /demo/review của máy chủ HermesQA. Không có bước build: ES module + CDN của Firebase.

   Ba nguồn dữ liệu (đều chỉ đọc; quyền ở firestore.rules):
     - hermesqa_runs      : run log do CLI/worker ghi (app/runlog.py). Một document = một lần review.
     - hermesqa_eval      : bảng benchmark do tools/publish_eval.py đẩy lên.
     - hermesqa_demo_jobs : job "Review thử" do app/demo.py ghi (tiến độ + log).
   Không có firebase-config.js -> trang vẫn mở được: chỉ tab "Review thử" hoạt động, tiến độ lấy bằng
   cách hỏi API /demo/jobs/<id> mỗi 2 giây. */

const CFG = window.HERMESQA || {};
const API = (CFG.apiBase || "").replace(/\/$/, "");
const COL = Object.assign({ runs: "hermesqa_runs", eval: "hermesqa_eval", jobs: "hermesqa_demo_jobs" }, CFG.collections || {});
const ROLES = ["SE", "QA", "DevOps"];
const SEV_ORDER = { critical: 0, high: 1, medium: 2, low: 3 };

const state = { runs: [], selected: null, run: null, evals: [], user: null, job: null, jobId: null,
                fb: null, unsubRun: null, unsubJob: null, poll: null };

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x) => (x == null ? "–" : (x * 100).toFixed(1) + " %");
const num = (x) => (x == null ? "–" : Number(x).toLocaleString("vi-VN"));
const when = (iso) => { if (!iso) return "–"; const d = new Date(iso); return isNaN(d) ? esc(iso) : d.toLocaleString("vi-VN", { hour12: false }); };

// ------------------------------------------------------------------ tabs
document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + b.dataset.tab));
  try { localStorage.setItem("hqa.tab", b.dataset.tab); } catch (_) {}
}));
try { const t = localStorage.getItem("hqa.tab"); if (t) document.querySelector(`#tabs button[data-tab="${t}"]`)?.click(); } catch (_) {}

function banner(msg) { const b = $("banner"); b.innerHTML = msg; b.classList.toggle("hidden", !msg); }

// ------------------------------------------------------------------ trình bày chung
function statusBadge(status, conclusion) {
  if (status === "running" || status === "queued") return `<span class="badge run">${status === "queued" ? "đang chờ" : "đang chạy"}</span>`;
  if (status === "failed") return `<span class="badge bad">lỗi</span>`;
  if (conclusion === "failure") return `<span class="badge bad">yêu cầu sửa</span>`;
  if (conclusion === "neutral") return `<span class="badge warn">có cảnh báo</span>`;
  return `<span class="badge ok">xong</span>`;
}

function stepTime(t, t0) {
  if (typeof t === "number") return `+${t}s`;            // app/demo.py ghi số giây kể từ lúc bắt đầu
  const d = new Date(t);                                   // app/runlog.py ghi ISO
  if (isNaN(d)) return esc(t);
  if (t0) { const s = (d - new Date(t0)) / 1000; if (!isNaN(s)) return `+${s.toFixed(0)}s`; }
  return d.toLocaleTimeString("vi-VN", { hour12: false });
}

const STEP_LABEL = (s) => {
  if (s === "static") return "công cụ tĩnh (sandbox)";
  if (s === "postprocess") return "gộp & lọc";
  if (s === "done") return "xong";
  if (s === "queued") return "trong hàng đợi";
  if (s === "start") return "bắt đầu";
  let m;
  if ((m = s.match(/^llm:(\w+)$/))) return `vai ${m[1]}`;
  if ((m = s.match(/^failed:(\w+)$/))) return `vai ${m[1]} lỗi`;
  if ((m = s.match(/^(\w+):(start|verify|done)$/))) return `vai ${m[1]} · ${{ start: "đang hỏi", verify: "kiểm tra sự thật", done: "xong" }[m[2]]}`;
  return s;
};

function timeline(steps, current, t0, running) {
  if (!steps || !steps.length) return "";
  const last = steps.length - 1;
  return `<ul class="timeline">${steps.map((s, i) =>
    `<li class="${running && i === last ? "cur" : ""}" title="${esc(s.detail || "")}">${esc(STEP_LABEL(s.step))}<small>${stepTime(s.t, t0)}</small></li>`).join("")}</ul>`;
}

function roleTable(roleStats, roles) {
  const names = [...new Set([...(roles || []), ...Object.keys(roleStats || {})])];
  if (!names.length) return `<p class="muted">Không gọi vai LLM nào (chỉ công cụ tĩnh).</p>`;
  const row = (r) => {
    const s = roleStats?.[r] || {};
    const keep = s.raw ? s.kept / s.raw : null;
    return `<tr><td><b>${esc(r)}</b>${s.failed ? ' <span class="badge bad">lỗi</span>' : ""}</td>
      <td class="num">${num(s.calls)}</td><td class="num">${num(s.raw)}</td><td class="num">${num(s.removed)}</td>
      <td class="num">${num(s.kept)}</td><td class="num">${num(s.final)}</td>
      <td><div class="bar" title="${pct(keep)}"><i style="width:${keep == null ? 0 : keep * 100}%"></i></div></td>
      <td class="num">${num(s.tokens_in)} / ${num(s.tokens_out)}</td></tr>`;
  };
  return `<div class="tbl"><table><thead><tr><th>Vai</th><th class="num">Lượt gọi</th><th class="num">Sinh ra</th><th class="num">Bị loại</th>
    <th class="num">Giữ</th><th class="num">Đăng</th><th>Tỷ lệ giữ</th><th class="num">Token vào / ra</th></tr></thead>
    <tbody>${names.map(row).join("")}</tbody></table></div>`;
}

function findingList(findings) {
  if (!findings || !findings.length) return `<p class="muted">Không có nhận xét.</p>`;
  const fs = [...findings].sort((a, b) => (SEV_ORDER[a.severity] ?? 9) - (SEV_ORDER[b.severity] ?? 9) || String(a.file).localeCompare(b.file) || a.line - b.line);
  return fs.map((f) => `<div class="finding">
      <div class="head"><span class="sev ${esc(f.severity)}">${esc(f.severity)}</span>
        <span class="loc">${esc(f.file)}:${esc(f.line)}</span><span class="title">${esc(f.title)}</span>
        <span class="badge">${esc(f.source === "llm" ? f.role : f.source)}</span>
        <span class="badge">${esc(f.category)}</span>
        ${f.confidence != null ? `<span class="muted">${Math.round(f.confidence * 100)} %</span>` : ""}
        ${f.anchor ? `<span class="muted" title="cách ghim">ghim: ${esc(f.anchor)}</span>` : ""}</div>
      ${f.existing_code ? `<pre>${esc(f.existing_code)}</pre>` : ""}
      <p>${esc(f.explanation)}</p>
      ${f.suggested_fix ? `<details><summary>Gợi ý sửa</summary><pre>${esc(f.suggested_fix)}</pre></details>` : ""}
    </div>`).join("");
}

// ------------------------------------------------------------------ tab Lần chạy
function renderRunList() {
  const el = $("run-list");
  if (!state.runs.length) { el.innerHTML = `<div class="empty">Chưa có lần chạy nào.</div>`; return; }
  el.innerHTML = state.runs.map((r) => `<div class="run-item ${r.run_id === state.selected ? "sel" : ""}" data-id="${esc(r.run_id)}">
      <div class="t"><span>${when(r.started_at)}</span>${statusBadge(r.status, r.conclusion)}</div>
      <div class="name">${esc(r.repo || "?")}${r.pr_number ? " #" + esc(r.pr_number) : ""} · ${esc(r.title || "")}</div>
      <div class="sub"><span>${esc(r.source || "")}</span><span>${num(r.counts?.final)} nhận xét</span>
        <span>${esc(r.model || "")}</span>${r.seconds ? `<span>${num(r.seconds)} s</span>` : ""}
        ${r.eval_case ? `<span class="badge">eval: ${esc(r.eval_case)}</span>` : ""}</div></div>`).join("");
  el.querySelectorAll(".run-item").forEach((d) => d.addEventListener("click", () => selectRun(d.dataset.id)));
}

function selectRun(id) {
  state.selected = id;
  renderRunList();
  if (state.unsubRun) { state.unsubRun(); state.unsubRun = null; }
  if (!state.fb) return;
  const { doc, onSnapshot } = state.fb.fs;
  state.unsubRun = onSnapshot(doc(state.fb.db, COL.runs, id), (snap) => {
    state.run = snap.exists() ? snap.data() : null;
    renderRunDetail();
  }, (e) => { $("run-detail").innerHTML = `<p class="muted">Không đọc được: ${esc(e.message)}</p>`; });
}

function renderRunDetail() {
  const r = state.run, el = $("run-detail");
  if (!r) { el.innerHTML = `<p class="muted">Không có dữ liệu.</p>`; return; }
  const running = r.status === "running" || r.status === "queued";
  const sev = r.counts?.by_severity || {};
  const bySev = ["critical", "high", "medium", "low"].filter((s) => sev[s]).map((s) => `<span class="sev ${s}">${s} ${sev[s]}</span>`).join(" · ");
  const tools = Object.entries(r.static_by_tool || {}).map(([t, n]) => `<span class="badge">${esc(t)}: ${n}</span>`).join(" ");
  const toolStates = Object.entries(r.tools || {}).filter(([, s]) => s?.state && s.state !== "ok").map(([t, s]) => `<span class="badge warn">${esc(t)}: ${esc(s.state)}</span>`).join(" ");
  el.innerHTML = `
    <h2>${esc(r.repo || "?")}${r.pr_number ? " #" + esc(r.pr_number) : ""} ${statusBadge(r.status, r.conclusion)}</h2>
    <div class="muted">${esc(r.title || "")}</div>
    <div class="kv"><span>bắt đầu <b>${when(r.started_at)}</b></span><span>nguồn <b>${esc(r.source)}</b></span>
      <span>model <b>${esc(r.model || "–")}</b></span><span>kiểm chứng <b>${esc(r.config?.verify_mode || "–")}</b></span>
      <span>công cụ tĩnh <b>${r.config?.skip_static ? "tắt" : "bật"}</b></span>
      ${r.seconds != null ? `<span>thời gian <b>${num(r.seconds)} s</b></span>` : ""}
      ${r.stats?.tokens_in != null ? `<span>token <b>${num(r.stats.tokens_in)} / ${num(r.stats.tokens_out)}</b></span>` : ""}
      ${r.head_sha ? `<span>commit <b>${esc(String(r.head_sha).slice(0, 8))}</b></span>` : ""}</div>
    ${timeline(r.steps, r.current, r.started_at, running)}
    <h3>Từng vai làm gì</h3>
    ${roleTable(r.role_stats, r.config?.roles)}
    ${tools || toolStates ? `<div class="kv"><span>công cụ tĩnh:</span> ${tools} ${toolStates}</div>` : ""}
    ${r.summaries && Object.keys(r.summaries).length ? `<details><summary>Tóm tắt của từng vai</summary>${Object.entries(r.summaries).map(([k, v]) => `<p><b>${esc(k)}</b>: ${esc(v)}</p>`).join("")}</details>` : ""}
    <h3>Nhận xét (${num(r.counts?.final ?? r.findings?.length)})${bySev ? ` <span class="muted">· ${bySev}</span>` : ""}</h3>
    ${findingList(r.findings)}
    ${r.findings_truncated ? `<p class="muted">Danh sách đã bị cắt bớt vì giới hạn kích thước document.</p>` : ""}
    ${r.removed?.length ? `<details><summary>${r.removed.length} nhận xét của LLM bị loại sau kiểm chứng</summary>${r.removed.map((x) =>
      `<div class="finding"><div class="head"><span class="loc">${esc(x.file)}:${esc(x.line)}</span><span class="title">${esc(x.title)}</span><span class="badge">${esc(x.role || "")}</span></div><p class="muted">căn cứ ${esc(x.ground)}: ${esc(x.check)}</p></div>`).join("")}</details>` : ""}
    ${r.skipped?.length ? `<details><summary>${r.skipped.length} file không được LLM review</summary>${r.skipped.map((s) => `<p><code>${esc(s.path)}</code> — ${esc(s.reason)} ${esc(s.detail || "")}</p>`).join("")}</details>` : ""}
    ${r.failed_roles?.length ? `<p><span class="badge bad">vai lỗi</span> ${esc(r.failed_roles.join(", "))}</p>` : ""}`;
}

// ------------------------------------------------------------------ tab Chấm điểm agent
function renderAgents() {
  const el = $("agents");
  const runs = state.runs.filter((r) => r.role_stats);
  const agg = {};
  for (const r of runs) for (const [role, s] of Object.entries(r.role_stats)) {
    const a = (agg[role] ||= { runs: 0, calls: 0, raw: 0, removed: 0, kept: 0, final: 0, tokens_in: 0, tokens_out: 0, failed: 0 });
    a.runs++;
    for (const k of Object.keys(a)) if (k !== "runs") a[k] += Number(s[k] || 0);
  }
  const tools = {};
  for (const r of state.runs) for (const [t, n] of Object.entries(r.static_by_tool || {})) tools[t] = (tools[t] || 0) + Number(n || 0);
  const order = (x) => { const i = ROLES.indexOf(x); return i < 0 ? 99 : i; };
  const roleRows = Object.entries(agg).sort(([a], [b]) => order(a) - order(b) || a.localeCompare(b)).map(([role, a]) => {
    const keep = a.raw ? a.kept / a.raw : null, post = a.kept ? a.final / a.kept : null;
    return `<tr><td><b>${esc(role)}</b></td><td class="num">${num(a.runs)}</td><td class="num">${num(a.calls)}</td><td class="num">${num(a.raw)}</td>
      <td class="num">${num(a.removed)}</td><td class="num">${num(a.kept)}</td><td class="num">${num(a.final)}</td>
      <td class="num">${pct(keep)}</td><td class="num">${pct(post)}</td>
      <td class="num">${num(a.calls ? Math.round((a.tokens_in + a.tokens_out) / a.calls) : null)}</td>
      <td class="num">${num(a.failed)}</td></tr>`;
  }).join("");

  const evalBlocks = state.evals.map((ev) => {
    const cfgs = Object.entries(ev.configs || {});
    const agents = [...new Set(cfgs.flatMap(([, c]) => Object.keys(c.by_agent || {})))].sort();
    if (!agents.length) return "";
    return `<h3>${esc(ev.name)} <span class="muted">· ${esc(ev.note || "")}</span></h3>
      <div class="tbl"><table><thead><tr><th>Agent / công cụ</th>${cfgs.map(([k]) => `<th class="num">${esc(k)}<br><small>đúng / tổng · precision</small></th>`).join("")}</tr></thead>
      <tbody>${agents.map((a) => `<tr><td><b>${esc(a)}</b></td>${cfgs.map(([, c]) => {
        const v = c.by_agent?.[a];
        return `<td class="num">${v ? `${v.tp} / ${v.findings} · ${pct(v.precision)}` : "–"}</td>`;
      }).join("")}</tr>`).join("")}</tbody></table></div>`;
  }).join("");

  el.innerHTML = `<div class="card">
    <h2>Từng agent đóng góp gì — từ ${num(runs.length)} lần chạy gần nhất</h2>
    <p class="muted">"Sinh ra" là số nhận xét vai đó trả về; "bị loại" là số bị bước kiểm tra sự thật hoặc bộ lọc phạm vi gạt đi;
      "giữ" là số còn lại; "đăng" là số lọt qua bước gộp với công cụ tĩnh và chính sách (độ tin cậy, mức nghiêm trọng, giới hạn comment).
      Run log chỉ biết hệ thống <em>nói gì</em>, không biết <em>đúng hay sai</em>, nên precision chỉ có ở bảng benchmark bên dưới.</p>
    ${roleRows ? `<div class="tbl"><table><thead><tr><th>Vai</th><th class="num">Lần chạy</th><th class="num">Lượt gọi</th><th class="num">Sinh ra</th><th class="num">Bị loại</th>
      <th class="num">Giữ</th><th class="num">Đăng</th><th class="num">Giữ / sinh</th><th class="num">Đăng / giữ</th><th class="num">Token / lượt</th><th class="num">Lỗi</th></tr></thead>
      <tbody>${roleRows}</tbody></table></div>` : `<p class="muted">Chưa có lần chạy nào có thống kê theo vai.</p>`}
    ${Object.keys(tools).length ? `<p>Công cụ tĩnh: ${Object.entries(tools).map(([t, n]) => `<span class="badge">${esc(t)}: ${num(n)}</span>`).join(" ")}</p>` : ""}
  </div>
  <div class="card" style="margin-top:16px">
    <h2>Precision theo agent trên bộ đo có đáp án</h2>
    <p class="muted">Một nhận xét là "đúng" khi khớp một lỗi trong đáp án (cùng file, lệch dòng trong ngưỡng, đúng loại).
      Recall theo agent không có nghĩa (một đáp án không "thuộc" vai nào) nên chỉ có precision.</p>
    ${evalBlocks || `<p class="muted">Chưa có bảng eval nào được đẩy lên (chạy <code>python tools/publish_eval.py</code>).</p>`}
  </div>`;
}

// ------------------------------------------------------------------ tab Benchmark
function renderEval() {
  const el = $("eval");
  if (!state.evals.length) { el.innerHTML = `<div class="card"><p class="muted">Chưa có bảng eval nào. Chạy <code>python tools/publish_eval.py</code> trên máy có kết quả eval.</p></div>`; return; }
  el.innerHTML = state.evals.map((ev) => {
    const cfgs = Object.entries(ev.configs || {});
    const cats = [...new Set(cfgs.flatMap(([, c]) => Object.keys(c.by_category || {})))].sort();
    return `<div class="card" style="margin-bottom:16px">
      <h2>${esc(ev.name)}</h2>
      <div class="kv"><span>${esc(ev.note || "")}</span><span>đáp án <b>${esc(ev.truth || "")}</b></span>
        <span>độ lệch dòng <b>${esc(ev.tolerance)}</b></span><span>đẩy lên <b>${when(ev.published_at)}</b></span></div>
      <div class="tbl"><table><thead><tr><th>Cấu hình</th><th class="num">Case</th><th class="num">TP</th><th class="num">FP</th><th class="num">FN</th>
        <th class="num">Precision</th><th class="num">Recall</th><th class="num">F1</th><th>F1</th><th class="num">s / PR</th><th class="num">token / PR</th></tr></thead>
        <tbody>${cfgs.map(([k, c]) => `<tr><td><b>${esc(k)}</b>${c.failed_runs ? ` <span class="badge warn">${c.failed_runs} lỗi</span>` : ""}</td>
          <td class="num">${num(c.cases)}</td><td class="num">${num(c.tp)}</td><td class="num">${num(c.fp)}</td><td class="num">${num(c.fn)}</td>
          <td class="num">${pct(c.precision)}</td><td class="num">${pct(c.recall)}</td><td class="num"><b>${pct(c.f1)}</b></td>
          <td><div class="bar"><i style="width:${(c.f1 || 0) * 100}%"></i></div></td>
          <td class="num">${num(Math.round(c.avg_seconds || 0))}</td><td class="num">${num(Math.round(c.avg_tokens || 0))}</td></tr>`).join("")}</tbody></table></div>
      ${cats.length ? `<details><summary>Bắt được theo loại lỗi</summary><div class="tbl"><table><thead><tr><th>Loại</th>${cfgs.map(([k]) => `<th class="num">${esc(k)}</th>`).join("")}</tr></thead>
        <tbody>${cats.map((cat) => `<tr><td>${esc(cat)}</td>${cfgs.map(([, c]) => { const v = c.by_category?.[cat]; return `<td class="num">${v ? `${v[0]} / ${v[1]}` : "–"}</td>`; }).join("")}</tr>`).join("")}</tbody></table></div></details>` : ""}
      ${cfgs.map(([k, c]) => c.missed?.length ? `<details><summary>${esc(k)}: ${c.missed_total ?? c.missed.length} lỗi bị bỏ sót</summary>${c.missed.map((m) =>
        `<p><code>${esc(m.case)}</code> — ${esc(m.file)}:${esc(m.line)} (${esc(m.category)}) <span class="muted">${esc(m.note || "")}</span></p>`).join("")}</details>` : "").join("")}
    </div>`;
  }).join("");
}

// ------------------------------------------------------------------ tab Review thử
let demoInfo = null;

async function loadDemoRepos() {
  const sel = $("demo-repo"), head = $("demo-head");
  try {
    const r = await fetch(API + "/demo/repos");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    demoInfo = await r.json();
  } catch (e) {
    sel.innerHTML = `<option value="">(không nối được API${API ? " " + esc(API) : ""})</option>`;
    $("demo-hint").innerHTML = `Không gọi được <code>${esc(API || location.origin)}/demo/repos</code>: ${esc(e.message)}. Máy chủ HermesQA có đang chạy và có <code>DEMO_CORS_ORIGINS</code> chứa origin này không?`;
    $("demo-submit").disabled = true;
    return;
  }
  if (!demoInfo.repos.length) {
    sel.innerHTML = `<option value="">(máy chủ chưa khai báo DEMO_REPOS)</option>`;
    $("demo-submit").disabled = true;
    return;
  }
  sel.innerHTML = demoInfo.repos.map((r) => `<option value="${esc(r.name)}">${esc(r.name)}</option>`).join("");
  const fill = () => {
    const repo = demoInfo.repos.find((r) => r.name === sel.value);
    const br = (repo?.branches || []).filter((b) => b !== $("demo-base").value);
    head.innerHTML = br.length ? br.map((b) => `<option>${esc(b)}</option>`).join("") : `<option value="">(không có nhánh)</option>`;
  };
  sel.addEventListener("change", fill); $("demo-base").addEventListener("input", fill); fill();
  $("demo-hint").innerHTML = `Mỗi người tối đa <b>${demoInfo.max_per_hour}</b> lần/giờ · công cụ tĩnh ${demoInfo.skip_static ? "<b>tắt</b> (máy chủ demo không có Docker)" : "<b>bật</b>"}${demoInfo.require_auth ? " · cần đăng nhập GitHub" : ""}.`;
  updateDemoGate();
}

function updateDemoGate() {
  const need = demoInfo ? demoInfo.require_auth : true;
  const ok = !need || !!state.user;
  $("demo-submit").disabled = !ok || !demoInfo?.repos?.length;
  $("demo-status").textContent = ok ? "" : "Đăng nhập GitHub (góc trên bên phải) để bật nút này.";
}

$("demo-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const body = { repo: $("demo-repo").value, head: $("demo-head").value, base: $("demo-base").value };
  const headers = { "Content-Type": "application/json" };
  if (state.user) headers.Authorization = "Bearer " + await state.user.getIdToken();
  $("demo-submit").disabled = true; $("demo-status").textContent = "đang gửi…";
  try {
    const r = await fetch(API + "/demo/review", { method: "POST", headers, body: JSON.stringify(body) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    $("demo-status").textContent = "";
    watchJob(data.job_id);
  } catch (e) {
    $("demo-status").textContent = "Từ chối: " + e.message;
  } finally { setTimeout(updateDemoGate, 1500); }
});

function watchJob(jobId) {
  state.jobId = jobId; state.job = null;
  if (state.unsubJob) { state.unsubJob(); state.unsubJob = null; }
  if (state.poll) { clearInterval(state.poll); state.poll = null; }
  renderJob();
  if (state.fb) {
    const { doc, onSnapshot } = state.fb.fs;
    state.unsubJob = onSnapshot(doc(state.fb.db, COL.jobs, jobId), (s) => { state.job = s.exists() ? s.data() : null; renderJob(); });
  } else {
    const tick = async () => {
      try { const r = await fetch(`${API}/demo/jobs/${jobId}`); if (r.ok) { state.job = await r.json(); renderJob(); } } catch (_) {}
      if (state.job && (state.job.status === "done" || state.job.status === "failed")) { clearInterval(state.poll); state.poll = null; }
    };
    tick(); state.poll = setInterval(tick, 2000);
  }
}

function renderJob() {
  const j = state.job, el = $("demo-job");
  if (!j) { el.innerHTML = `<p class="muted">Đang tạo job ${esc(state.jobId)}…</p>`; return; }
  const running = j.status === "queued" || j.status === "running";
  const run = state.runs.find((r) => r.run_id === j.job_id);
  const liveSteps = run?.steps?.length ? run.steps : j.steps;     // run log chi tiết hơn log của cli nếu có
  el.innerHTML = `<h2>Job ${esc(j.job_id)} ${statusBadge(j.status)}</h2>
    <div class="kv"><span>repo <b>${esc(j.repo)}</b></span><span>${esc(j.base)} → <b>${esc(j.head)}</b></span><span>người chạy <b>${esc(j.user)}</b></span>
      ${j.seconds != null ? `<span>đã chạy <b>${num(j.seconds)} s</b></span>` : ""}
      ${j.tokens_in ? `<span>token <b>${num(j.tokens_in)} / ${num(j.tokens_out)}</b></span>` : ""}</div>
    ${timeline(liveSteps, j.current, run?.started_at || j.started_at, running)}
    ${j.error ? `<p><span class="badge ${j.status === "failed" ? "bad" : "warn"}">lưu ý</span> ${esc(j.error)}</p>` : ""}
    ${run ? `<p><a href="#" id="job-open-run">Mở run log đầy đủ của lần chạy này</a></p>` : ""}
    ${j.status === "done" ? `<h3>${num(j.findings_count)} nhận xét</h3>${findingList(j.findings)}` : ""}
    ${j.report_md ? `<details><summary>Báo cáo Markdown</summary><pre>${esc(j.report_md)}</pre></details>` : ""}
    ${j.log_tail?.length ? `<details ${running ? "open" : ""}><summary>Log của tiến trình</summary><pre>${esc(j.log_tail.join("\n"))}</pre></details>` : ""}`;
  el.querySelector("#job-open-run")?.addEventListener("click", (e) => {
    e.preventDefault(); document.querySelector('#tabs button[data-tab="runs"]').click(); selectRun(j.job_id);
  });
}

// ------------------------------------------------------------------ đăng nhập
function renderWho() {
  const el = $("who");
  if (!state.fb) { el.innerHTML = `<span class="muted">chưa cấu hình Firebase</span>`; return; }
  if (state.user) {
    el.innerHTML = `${state.user.photoURL ? `<img src="${esc(state.user.photoURL)}" alt="">` : ""}<span>${esc(state.user.displayName || state.user.email || "đã đăng nhập")}</span>
      <button class="ghost" id="logout">Đăng xuất</button>`;
    el.querySelector("#logout").addEventListener("click", () => state.fb.auth.signOut(state.fb.authInst));
  } else {
    el.innerHTML = `<button class="ghost" id="login">Đăng nhập GitHub</button>`;
    el.querySelector("#login").addEventListener("click", async () => {
      try { await state.fb.auth.signInWithPopup(state.fb.authInst, new state.fb.auth.GithubAuthProvider()); }
      catch (e) { banner(`Không đăng nhập được: ${esc(e.code || e.message)}`); }
    });
  }
}

// ------------------------------------------------------------------ khởi động
async function main() {
  $("source-badge").textContent = API ? `API: ${API}` : "API: cùng origin";
  loadDemoRepos();
  if (!CFG.firebase) {
    banner(`Chưa có <code>firebase-config.js</code> (xem <code>docs/FIREBASE_DEMO.md</code>). Chỉ tab "Review thử" hoạt động, tiến độ lấy bằng cách hỏi API mỗi 2 giây.`);
    $("run-list").innerHTML = `<div class="empty">Cần Firestore để xem lịch sử.</div>`;
    renderAgents(); renderEval(); renderWho(); updateDemoGate();
    return;
  }
  const [appM, fsM, authM] = await Promise.all([
    import("https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js"),
    import("https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js"),
    import("https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js"),
  ]);
  const app = appM.initializeApp(CFG.firebase);
  const db = fsM.getFirestore(app);
  const authInst = authM.getAuth(app);
  state.fb = { db, fs: fsM, auth: authM, authInst };
  renderWho();
  authM.onAuthStateChanged(authInst, (u) => { state.user = u; renderWho(); updateDemoGate(); });

  const { collection, query, orderBy, limit, onSnapshot } = fsM;
  onSnapshot(query(collection(db, COL.runs), orderBy("started_at", "desc"), limit(60)), (snap) => {
    state.runs = snap.docs.map((d) => ({ run_id: d.id, ...d.data() }));
    renderRunList(); renderAgents();
    if (!state.selected && state.runs.length) selectRun(state.runs[0].run_id);
    if (state.job) renderJob();
  }, (e) => { banner(`Không đọc được Firestore (${esc(e.code || e.message)}) — kiểm tra firestore.rules và projectId.`); });
  onSnapshot(collection(db, COL.eval), (snap) => {
    state.evals = snap.docs.map((d) => ({ name: d.id, ...d.data() })).sort((a, b) => String(b.published_at).localeCompare(String(a.published_at)));
    renderEval(); renderAgents();
  }, () => {});
}

main().catch((e) => banner(`Lỗi khởi động: ${esc(e.message)}`));
