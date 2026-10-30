/* Trang "Kiểm tra code" cho người dùng phổ thông. Không cần build, không cần Firebase:
   gửi POST /demo/review rồi hỏi /demo/jobs/<id> mỗi 2 giây cho tới khi xong (app/demo.py). */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (x) => (x == null ? "–" : Number(x).toLocaleString("vi-VN"));
const when = (iso) => { if (!iso) return ""; const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleString("vi-VN", { hour12: false, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }); };

const SEV = { critical: "Nghiêm trọng", high: "Cần sửa", medium: "Nên sửa", low: "Gợi ý" };
const SEV_ORDER = ["critical", "high", "medium", "low"];
const CAT = {
  security: "Bảo mật", bug: "Lỗi logic", test: "Kiểm thử", devops: "Vận hành", style: "Cách viết",
  performance: "Hiệu năng", docs: "Tài liệu", maintainability: "Bảo trì",
};
const ROLE = { SE: "kỹ sư phần mềm", QA: "kiểm thử", DevOps: "vận hành" };
const ICON_CHECK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12l5 5 9-10"/></svg>`;
const ICON_PIN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 21s7-6.1 7-11a7 7 0 1 0-14 0c0 4.9 7 11 7 11z"/><circle cx="12" cy="10" r="2.5"/></svg>`;
const ICON_X = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>`;

const state = { mode: "github", jobId: null, job: null, poll: null, sample: null, filterSev: null, filterCat: null };

// ------------------------------------------------------------------ chọn cách đưa code vào
document.querySelectorAll(".segmented button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll(".segmented button").forEach((x) => {
    const on = x.dataset.mode === mode;
    x.classList.toggle("active", on);
    x.setAttribute("aria-selected", on ? "true" : "false");
  });
  document.querySelectorAll(".mode").forEach((m) => m.classList.toggle("hidden", m.id !== "mode-" + mode));
  try { localStorage.setItem("hqa.mode", mode); } catch (_) {}
}
try { const m = localStorage.getItem("hqa.mode"); if (m && $("mode-" + m)) setMode(m); } catch (_) {}

function setStatus(msg, isError) {
  const el = $("status");
  el.textContent = msg || "";
  el.classList.toggle("error", !!isError);
}
function setBusy(busy) {
  const b = $("submit");
  b.disabled = busy;
  b.querySelector(".spinner").classList.toggle("hidden", !busy);
  b.querySelector(".label").textContent = busy ? "Đang kiểm tra…" : "Kiểm tra code";
}

// ------------------------------------------------------------------ gửi yêu cầu
$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { source: state.mode };
  if (state.mode === "github") {
    body.url = $("url").value.trim();
    body.base = $("base").value.trim();
    if (!/^https?:\/\/(www\.)?github\.com\/[^/\s]+\/[^/\s]+/.test(body.url)) return setStatus("Hãy dán một link GitHub dạng https://github.com/tên/repo…", true);
  } else if (state.mode === "code") {
    body.text = $("code").value;
    body.filename = $("filename").value.trim();
    if (!body.text.trim()) return setStatus("Chưa có đoạn code nào để kiểm tra.", true);
  } else {
    body.text = $("diff").value;
    if (!body.text.trim()) return setStatus("Chưa có nội dung diff.", true);
  }
  await submit(body);
});

$("sample").addEventListener("click", () => state.sample && submit({ source: "repo", ...state.sample }));

async function submit(body) {
  setStatus("");
  setBusy(true);
  try {
    const r = await fetch("/demo/review", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      setStatus(friendlyError(r.status, d), true);
      setBusy(false);
      return;
    }
    const { job_id } = await r.json();
    openJob(job_id);
  } catch (err) {
    setStatus("Không kết nối được với máy chủ: " + err.message, true);
    setBusy(false);
  }
}

function friendlyError(status, d) {
  const detail = Array.isArray(d.detail) ? d.detail.map((x) => x.msg).join("; ") : d.detail;
  if (status === 429) return detail || "Bạn đã dùng hết số lượt trong giờ này, thử lại sau ít phút.";
  if (status === 409) return "Đang có một lượt kiểm tra khác chạy, đợi xong rồi thử lại.";
  if (status === 401) return "Máy chủ yêu cầu đăng nhập cho tính năng này.";
  return detail || `Máy chủ từ chối (mã ${status}).`;
}

// ------------------------------------------------------------------ theo dõi job
function stopPoll() { if (state.poll) { clearTimeout(state.poll); state.poll = null; } }

async function openJob(id) {
  stopPoll();
  state.jobId = id;
  state.filterSev = state.filterCat = null;
  if (location.hash !== "#job=" + id) history.replaceState(null, "", "#job=" + id);
  $("result").classList.add("hidden");
  $("progress").classList.remove("hidden");
  renderProgress({ status: "queued", steps: [] });
  $("progress").scrollIntoView({ behavior: "smooth", block: "start" });
  document.querySelectorAll(".hist-item").forEach((b) => b.classList.toggle("sel", b.dataset.id === id));
  await tick();
}

async function tick() {
  let job;
  try {
    const r = await fetch(`/demo/jobs/${state.jobId}`);
    if (!r.ok) {
      $("progress").innerHTML = `<div class="card-body"><div class="alert error">Không tìm thấy lượt kiểm tra này.</div></div>`;
      setBusy(false);
      return;
    }
    job = await r.json();
  } catch (err) {
    state.poll = setTimeout(tick, 3000);       // mất mạng tạm thời: thử lại
    return;
  }
  state.job = job;
  renderProgress(job);
  if (job.status === "done" || job.status === "failed") {
    stopPoll();
    setBusy(false);
    if (job.status === "done") renderResult(job);
    loadHistory();
  } else {
    state.poll = setTimeout(tick, 2000);
  }
}

const STEP_LABEL = (step, detail) => {
  let m;
  if (step === "queued") return "Đang chờ đến lượt";
  if (step === "start") return "Bắt đầu";
  if (step === "fetch") return detail ? "Chuẩn bị code: " + detail : "Chuẩn bị code cần xem";
  if (step === "static") return "Quét bằng các công cụ kiểm tra tự động";
  if (step === "static:done") return "Quét tự động xong" + (detail ? ` (${detail})` : "");
  if (step === "stale") return "Tìm khoá hết hạn và code lỗi thời";
  if (step === "postprocess") return "Gộp và lọc nhận xét";
  if (step === "done") return "Tổng hợp kết quả" + (detail ? ` (${detail})` : "");
  if ((m = step.match(/^llm:(\w+)$/))) return `Đọc code dưới góc nhìn ${ROLE[m[1]] || m[1]}`;
  if ((m = step.match(/^failed:(\w+)$/))) return `Góc nhìn ${ROLE[m[1]] || m[1]} gặp lỗi, bỏ qua`;
  if ((m = step.match(/^(\w+):(start|verify|done)$/))) return `Góc nhìn ${ROLE[m[1]] || m[1]}: ${{ start: "đang đọc", verify: "kiểm tra lại nhận xét", done: "xong" }[m[2]]}`;
  return step;
};

function renderProgress(job) {
  const running = job.status === "running" || job.status === "queued";
  const steps = job.steps || [];
  const last = steps.length - 1;
  const items = steps.map((s, i) => {
    const bad = /^failed:/.test(s.step);
    const cls = bad ? "bad" : running && i === last ? "cur" : "ok";
    const ico = bad ? ICON_X : cls === "cur" ? "" : ICON_CHECK;
    return `<li class="${cls}"><span class="ico">${ico}</span><span>${esc(STEP_LABEL(s.step, s.detail))}</span><span class="t">${s.t != null ? "+" + s.t + "s" : ""}</span></li>`;
  });
  if (!steps.length) items.push(`<li class="cur"><span class="ico"></span><span>Đang chờ đến lượt</span></li>`);
  const pill = running ? `<span class="tag run"><span class="dot"></span>đang chạy</span>`
    : job.status === "failed" ? `<span class="tag bad">thất bại</span>` : `<span class="tag ok">hoàn tất</span>`;
  const title = running ? "Đang kiểm tra…" : job.status === "failed" ? "Không kiểm tra được" : "Đã kiểm tra xong";
  const track = running ? "indeterminate" : job.status === "failed" ? "failed" : "done";
  const sub = [job.label, job.title].filter(Boolean).map(esc).join(" · ");
  $("progress").innerHTML = `<div class="card-head"><div><h2>${title}</h2>${sub ? `<p class="sub">${sub}</p>` : ""}</div>${pill}</div>
    <div class="card-body">
      <div class="progress-track ${track}"><i></i></div>
      <ul class="timeline">${items.join("")}</ul>
      ${job.status === "failed" ? `<div class="alert error">${esc(job.error || "Lỗi không rõ. Hãy thử lại hoặc chọn đầu vào khác.")}</div>` : ""}
      ${running ? `<p class="hint">Thường mất từ 20 giây đến 2 phút tuỳ lượng code. Bạn có thể để tab này mở, kết quả sẽ tự hiện.</p>` : ""}
    </div>`;
}

// ------------------------------------------------------------------ kết quả
function renderResult(job) {
  const fs = (job.findings || []).slice().sort((a, b) =>
    (SEV_ORDER.indexOf(a.severity) + 9) % 9 - (SEV_ORDER.indexOf(b.severity) + 9) % 9 || String(a.file).localeCompare(b.file) || a.line - b.line);
  const bySev = {}, byCat = {};
  for (const f of fs) { bySev[f.severity] = (bySev[f.severity] || 0) + 1; byCat[f.category] = (byCat[f.category] || 0) + 1; }
  const total = fs.length;

  let lead;
  if (!total) lead = `<p class="lead-result"><b>Không tìm thấy vấn đề đáng kể</b> trong phần code này.</p>
    <p class="hint">Điều đó không có nghĩa code hoàn hảo: hệ thống chỉ xem phần được đưa vào và chỉ báo khi khá chắc.</p>`;
  else {
    const parts = SEV_ORDER.filter((s) => bySev[s]).map((s) => `${bySev[s]} ${SEV[s].toLowerCase()}`);
    lead = `<p class="lead-result">Tìm thấy <b>${total} vấn đề</b>: ${parts.join(", ")}.</p>`;
  }
  const tiles = SEV_ORDER.map((s) => `<div class="stat ${s} ${bySev[s] ? "" : "dim"}"><div class="k">${SEV[s]}</div><div class="v">${bySev[s] || 0}</div></div>`).join("");
  const stack = SEV_ORDER.filter((s) => bySev[s]).map((s) => `<i class="${s}" style="width:${(bySev[s] / total) * 100}%" title="${SEV[s]}: ${bySev[s]}"></i>`).join("");

  const sevChips = SEV_ORDER.filter((s) => bySev[s]).map((s) => `<button type="button" class="chip ${state.filterSev === s ? "on" : ""}" data-sev="${s}">${SEV[s]} <span class="cnt">${bySev[s]}</span></button>`).join("");
  const catChips = Object.keys(byCat).sort().map((c) => `<button type="button" class="chip ${state.filterCat === c ? "on" : ""}" data-cat="${esc(c)}">${esc(CAT[c] || c)} <span class="cnt">${byCat[c]}</span></button>`).join("");

  const shown = fs.filter((f) => (!state.filterSev || f.severity === state.filterSev) && (!state.filterCat || f.category === state.filterCat));
  const list = shown.length ? shown.map((f) => findingCard(f, job)).join("") : (total ? `<div class="empty">Không có nhận xét nào khớp bộ lọc.</div>` : "");

  const tokens = (job.tokens_in || 0) + (job.tokens_out || 0);
  const tech = `<details class="block" style="margin-top:16px"><summary>Chi tiết kỹ thuật</summary>
    <div class="kv" style="margin-top:8px">
      <div>Thời gian<b>${num(job.seconds)} giây</b></div>
      <div>Token AI<b>${num(tokens)}</b></div>
      <div>Công cụ quét tự động<b>${job.skip_static ? "tắt" : "bật"}</b></div>
      ${job.head_sha ? `<div>Commit<b>${esc(String(job.head_sha).slice(0, 8))}</b></div>` : ""}
      ${job.base || job.head ? `<div>So sánh<b>${esc(job.base || "?")} → ${esc(job.head || "?")}</b></div>` : ""}
    </div>
    <p class="hint"><a href="/demo/jobs/${esc(job.job_id)}/report.md">Tải báo cáo đầy đủ (Markdown)</a>
      · <a href="/dashboard/runs/${esc(job.job_id)}">Xem trên trang kỹ thuật</a></p>
    ${job.log_tail?.length ? `<details style="margin-top:8px"><summary class="small">Nhật ký chạy (${job.log_tail.length} dòng cuối)</summary><pre>${esc(job.log_tail.join("\n"))}</pre></details>` : ""}
  </details>`;

  $("result").innerHTML = `<div class="card-head"><div><h2>Kết quả</h2></div>${total ? `<span class="tag">${total} nhận xét</span>` : ""}</div>
    <div class="card-body">${lead}
    ${total ? `<div class="summary-tiles">${tiles}</div><div class="stack" aria-hidden="true">${stack}</div>` : ""}
    ${job.error ? `<div class="alert warn">${esc(job.error)}</div>` : ""}
    ${total ? `<div class="filters"><span>Lọc:</span>${sevChips}<span class="sep"></span>${catChips}${state.filterSev || state.filterCat ? `<button type="button" class="chip" data-clear="1">Bỏ lọc</button>` : ""}</div>` : ""}
    ${list}${tech}</div>`;
  $("result").classList.remove("hidden");
  $("result").querySelectorAll(".chip").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.clear) state.filterSev = state.filterCat = null;
    else if (b.dataset.sev) state.filterSev = state.filterSev === b.dataset.sev ? null : b.dataset.sev;
    else if (b.dataset.cat) state.filterCat = state.filterCat === b.dataset.cat ? null : b.dataset.cat;
    renderResult(job);
  }));
}

// Mô tả vị trí nhận xét bằng lời cho người không quen đọc "file:line".
//   - code dán vào, không đặt tên file: "Dòng 4 của đoạn code bạn dán" (tên file chỉ là tên tạm do hệ thống đoán)
//   - diff dán vào / GitHub:            "Dòng 4 · file app/x.py"
function whereText(f, job) {
  const src = job?.source;
  const guessed = src === "code" && !/\(/.test(job?.label || "");   // label "Đoạn code dán vào (tên)" khi người dùng tự đặt tên
  const line = Number(f.line) > 0 ? `Dòng ${Number(f.line)}` : "";
  if (src === "code") {
    const what = guessed ? "của đoạn code bạn dán" : `· file ${esc(f.file)}`;
    const note = guessed ? ` <span class="muted">(hệ thống tạm gọi là ${esc(f.file)})</span>` : "";
    return `${line ? `<b>${line}</b> ${what}` : `Toàn bộ đoạn code bạn dán`}${note}`;
  }
  const changed = src === "diff" ? " trong phần thay đổi" : "";
  return line ? `<b>${line}</b> · file <b>${esc(f.file)}</b>${changed}` : `Cả file <b>${esc(f.file)}</b>${changed}`;
}

// Với code dán vào, lấy đúng dòng đó từ ô nhập để người dùng thấy ngay (chỉ khi AI không trích sẵn).
function sourceLine(f, job) {
  if (job?.source !== "code" || !(Number(f.line) > 0)) return "";
  const text = $("code")?.value || "";
  if (!text) return "";
  const ln = text.replace(/\r\n/g, "\n").split("\n")[Number(f.line) - 1];
  if (ln == null || !ln.trim()) return "";
  return `<pre class="src-line"><span class="ln">${Number(f.line)}</span>${esc(ln)}</pre>`;
}

function findingCard(f, job) {
  const who = f.source === "llm" || !f.source ? `AI, góc nhìn ${ROLE[f.role] || f.role || "?"}` : `công cụ ${f.source}`;
  const conf = f.confidence != null ? `độ chắc chắn ${Math.round(f.confidence * 100)} %` : "";
  const fix = (f.suggested_fix || "").trim();
  const fixIsCode = /\n/.test(fix) || /[{};=()]/.test(fix) && fix.length < 400;
  const code = f.existing_code ? `<pre>${esc(f.existing_code)}</pre>` : sourceLine(f, job);
  return `<article class="finding ${esc(f.severity)}">
    <header><span class="sev ${esc(f.severity)}">${SEV[f.severity] || esc(f.severity)}</span>
      <span class="cat">${esc(CAT[f.category] || f.category)}</span></header>
    <h4>${esc(f.title)}</h4>
    <p class="where">${ICON_PIN}${whereText(f, job)}</p>
    ${code}
    <p><b>Vấn đề:</b> ${esc(f.explanation)}</p>
    ${fix ? `<div class="fix"><b>Cách sửa:</b>${fixIsCode ? `<pre>${esc(fix)}</pre>` : ` ${esc(fix)}`}</div>` : ""}
    <div class="meta"><span>Phát hiện bởi ${esc(who)}</span>${conf ? `<span>${conf}</span>` : ""}${f.anchor ? `<span>ghim theo ${esc(f.anchor)}</span>` : ""}</div>
  </article>`;
}

// ------------------------------------------------------------------ lịch sử + ví dụ mẫu
async function loadHistory() {
  const el = $("history");
  try {
    const r = await fetch("/demo/jobs?limit=15");
    const { jobs } = await r.json();
    if (!jobs.length) { el.innerHTML = `<div class="empty">Chưa có lần kiểm tra nào. Hãy thử ngay bên trái.</div>`; return; }
    el.innerHTML = `<div class="hist">${jobs.map((j) => {
      const sev = j.by_severity || {};
      const parts = SEV_ORDER.filter((s) => sev[s]).map((s) => `${sev[s]} ${SEV[s].toLowerCase()}`).join(", ");
      const st = j.status === "failed" ? "bad" : j.status === "done" ? "ok" : "run";
      const count = j.status === "done" ? (j.findings_count ? `${j.findings_count} vấn đề${parts ? " (" + parts + ")" : ""}` : "không có vấn đề")
        : j.status === "failed" ? (j.error ? esc(j.error).slice(0, 80) : "lỗi") : "đang chạy…";
      return `<button type="button" class="hist-item ${j.job_id === state.jobId ? "sel" : ""}" data-id="${esc(j.job_id)}">
        <span class="st ${st}" aria-hidden="true"></span>
        <span class="name">${esc(j.label || j.repo || "?")}</span><span class="when">${when(j.queued_at)}</span>
        <span class="sub"><span>${count}</span>${j.seconds ? `<span>${num(j.seconds)} s</span>` : ""}</span></button>`;
    }).join("")}</div>`;
    el.querySelectorAll(".hist-item").forEach((b) => b.addEventListener("click", () => openJob(b.dataset.id)));
  } catch (err) {
    el.innerHTML = `<div class="empty">Không tải được lịch sử: ${esc(err.message)}</div>`;
  }
}

async function loadInfo() {
  try {
    const r = await fetch("/demo/repos");
    const d = await r.json();
    $("source-badge").textContent = `tối đa ${d.max_per_hour} lượt/giờ mỗi người · công cụ quét tự động ${d.skip_static ? "tắt" : "bật"}`;
    const repo = (d.repos || []).find((x) => x.branches?.length);
    if (repo) {
      const head = repo.branches.find((b) => b.startsWith("case/")) || repo.branches.find((b) => b !== "main" && b !== "master");
      if (head) { state.sample = { repo: repo.name, head, base: "" }; $("sample").classList.remove("hidden"); }
    }
  } catch (_) { /* không có thông tin cũng không sao */ }
}

loadInfo();
loadHistory();
const m = location.hash.match(/^#job=([0-9a-f]{12})$/);
if (m) openJob(m[1]);
