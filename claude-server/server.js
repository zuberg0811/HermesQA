// claude-server/server.js — OpenAI-compatible server bọc Claude Code headless (Claude Agent SDK) để hermesQA (hoặc bất kỳ
// client OpenAI SDK nào) gọi như một LLM bình thường, dùng tài khoản đã `claude login`.
//
// Chạy:  cd claude-server && npm install && npm start   -> http://localhost:3100/v1
//
// hermesQA .env:
//   AGENT_BACKEND=openai_compat
//   LLM_BASE_URL=http://localhost:3100/v1
//   LLM_API_KEY=none
//   LLM_MODEL=claude-opus-5-5
// hoặc dùng profile đã khai báo trong .env của hermesQA: --profile claudecode (LLM_CLAUDECODE_*)
//
// Endpoint:
//   POST /v1/chat/completions   { model, messages:[{role:"system"|"user"|"assistant", content}], ... }
//                               -> { choices:[{ message:{ role:"assistant", content } }], usage }
//   GET  /v1/models
//   GET  /health
//
// "pre" = preset: system prompt của Claude Code (preset claude_code) + nối thêm system message của client
// (SKILL.md của từng vai). Đặt SYSTEM_MODE=custom để chỉ dùng system message của client.

import http from "node:http";
import crypto from "node:crypto";
import { query } from "@anthropic-ai/claude-agent-sdk";

const PORT = Number(process.env.OPENAI_PORT) || 3100;
const DEFAULT_MODEL = process.env.CLAUDE_MODEL || "claude-opus-5-5";
const EFFORT = process.env.CLAUDE_EFFORT || "medium"; // low | medium | high | xhigh | max
const SYSTEM_MODE = (process.env.SYSTEM_MODE || "preset").toLowerCase(); // preset | custom
const CONCURRENCY = Number(process.env.CONCURRENCY) || 2;
const REQUEST_TIMEOUT_MS = Number(process.env.REQUEST_TIMEOUT_MS) || 300_000; // huỷ query Claude nếu quá lâu
const TOOLS = (process.env.CLAUDE_TOOLS || "").split(",").map((s) => s.trim()).filter(Boolean); // vd: WebFetch
const JSON_ONLY = (process.env.JSON_ONLY || "auto").toLowerCase(); // auto | true | false
const ALLOWED_MODELS = new Set(
  (process.env.ALLOWED_MODELS || "claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5,claude-opus-5,claude-sonnet-5,claude-fable-5-1")
    .split(",").map((s) => s.trim()).filter(Boolean)
);

// ---- Chuyển messages OpenAI -> (system, prompt) ----
function contentToText(c) {
  if (typeof c === "string") return c;
  if (Array.isArray(c)) return c.map((p) => (typeof p === "string" ? p : p?.text || "")).join("\n");
  return c == null ? "" : String(c);
}

function splitMessages(messages) {
  const systems = [];
  const turns = [];
  for (const m of messages) {
    const text = contentToText(m.content);
    if (m.role === "system" || m.role === "developer") systems.push(text);
    else turns.push({ role: m.role, text });
  }
  // SDK nhận 1 prompt string: nếu client gửi nhiều lượt, ghép lại có nhãn; thường hermesQA chỉ gửi 1 user.
  let prompt;
  if (turns.length === 1 && turns[0].role === "user") prompt = turns[0].text;
  else prompt = turns.map((t) => `[${t.role}]\n${t.text}`).join("\n\n");
  return { system: systems.join("\n\n").trim(), prompt };
}

// ---- Làm sạch text trả về: bỏ ```json fences; nếu cần JSON thì trích đúng object ----
function cleanText(text, wantJson) {
  let s = String(text ?? "").replace(/\r\n?/g, "\n").trim();
  s = s.replace(/^```(?:json)?\s*\n?/i, "").replace(/\n?```\s*$/i, "").trim();
  if (wantJson) {
    const a = s.indexOf("{");
    const b = s.lastIndexOf("}");
    if (a !== -1 && b > a) return s.slice(a, b + 1); // client tự parse/retry nếu JSON hỏng
  }
  return s;
}

// ---- Gọi Claude ----
async function complete({ model, system, prompt, id }) {
  const systemPrompt =
    SYSTEM_MODE === "custom"
      ? system || undefined
      : { type: "preset", preset: "claude_code", append: system || undefined };

  let text = "";
  let lastAssistantText = "";
  let usage = { prompt_tokens: 0, completion_tokens: 0 };
  let costUsd = null;
  let error = "";
  let stopReason = "stop";
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), REQUEST_TIMEOUT_MS);

  try {
  for await (const msg of query({
    prompt,
    options: {
      model,
      systemPrompt,
      effort: EFFORT,
      tools: TOOLS, // [] = thuần LLM, không tool
      allowedTools: TOOLS,
      settingSources: [], // không nạp CLAUDE.md / hooks / settings của máy
      strictMcpConfig: true, // không nạp MCP server bên ngoài
      maxTurns: TOOLS.length ? 8 : 1,
      persistSession: false, // không ghi transcript vào ~/.claude
      abortController: abort,
    },
  })) {
    if (msg.type === "system" && msg.subtype !== "init") {
      // api_retry, v.v. — để biết vì sao một request chậm
      console.warn(`[${id}] claude ${msg.subtype}${msg.error ? ": " + JSON.stringify(msg.error).slice(0, 200) : ""}${msg.attempt ? ` (lần ${msg.attempt}/${msg.max_retries ?? "?"}, đợi ${msg.retry_delay_ms ?? "?"}ms)` : ""}`);
    }
    if (msg.type === "assistant") {
      const t = (msg.message.content || []).filter((b) => b.type === "text").map((b) => b.text).join("");
      if (t.trim()) lastAssistantText = t;
      if (msg.message.stop_reason === "max_tokens") stopReason = "length";
    }
    if (msg.type === "result") {
      costUsd = msg.total_cost_usd ?? null;
      const u = msg.usage || {};
      usage = {
        prompt_tokens: (u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0),
        completion_tokens: u.output_tokens || 0,
      };
      if (msg.subtype === "success") text = msg.result || "";
      else error = msg.errors?.length ? msg.errors.join("; ") : msg.subtype;
    }
  }
  } catch (err) {
    if (abort.signal.aborted) throw new Error(`Claude không trả lời trong ${REQUEST_TIMEOUT_MS / 1000}s (timeout)`);
    throw err;
  } finally {
    clearTimeout(timer);
  }
  if (abort.signal.aborted) throw new Error(`Claude không trả lời trong ${REQUEST_TIMEOUT_MS / 1000}s (timeout)`);
  if (!text) text = lastAssistantText;
  if (!text && error) throw new Error(error);
  return { text, usage, costUsd, stopReason };
}

// ---- Semaphore ----
let running = 0;
const waiters = [];
async function withSlot(fn) {
  if (running >= CONCURRENCY) await new Promise((r) => waiters.push(r));
  running++;
  try {
    return await fn();
  } finally {
    running--;
    waiters.shift()?.();
  }
}

// ---- HTTP ----
async function readJson(req) {
  let body = "";
  for await (const chunk of req) body += chunk;
  return JSON.parse(body || "{}");
}

function json(res, code, data) {
  res.writeHead(code, { "Content-Type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(data));
}

function openaiError(res, code, message, type = "invalid_request_error") {
  json(res, code, { error: { message, type, code: null, param: null } });
}

http
  .createServer(async (req, res) => {
    const url = new URL(req.url, "http://x");
    const p = url.pathname.replace(/\/+$/, "");

    if (req.method === "GET" && p === "/health") return json(res, 200, { ok: true, running, waiting: waiters.length });

    if (req.method === "GET" && p === "/v1/models") {
      return json(res, 200, {
        object: "list",
        data: [...ALLOWED_MODELS].map((id) => ({ id, object: "model", created: 0, owned_by: "anthropic" })),
      });
    }

    if (req.method === "POST" && p === "/v1/chat/completions") {
      let body;
      try {
        body = await readJson(req);
      } catch {
        return openaiError(res, 400, "Body phải là JSON");
      }
      if (!Array.isArray(body.messages) || !body.messages.length) return openaiError(res, 400, "Thiếu messages");
      if (body.stream) return openaiError(res, 400, "stream=true chưa hỗ trợ, gọi không stream");

      const model = ALLOWED_MODELS.has(body.model) ? body.model : DEFAULT_MODEL;
      const { system, prompt } = splitMessages(body.messages);
      const wantJson =
        JSON_ONLY === "true" ||
        (JSON_ONLY === "auto" &&
          (body.response_format?.type === "json_object" || /JSON object/i.test(system + "\n" + prompt)));

      const id = "chatcmpl-" + crypto.randomUUID().replace(/-/g, "").slice(0, 24);
      const t0 = Date.now();
      console.log(`[${id}] ${model} · system ${system.length} ký tự · prompt ${prompt.length} ký tự · đang chạy ${running}/${CONCURRENCY}`);
      try {
        const r = await withSlot(() => complete({ model, system, prompt, id }));
        const content = cleanText(r.text, wantJson);
        console.log(`[${id}] xong ${Date.now() - t0}ms · in=${r.usage.prompt_tokens} out=${r.usage.completion_tokens} · $${r.costUsd ?? "?"}`);
        return json(res, 200, {
          id,
          object: "chat.completion",
          created: Math.floor(Date.now() / 1000),
          model,
          choices: [{ index: 0, message: { role: "assistant", content }, finish_reason: r.stopReason, logprobs: null }],
          usage: { ...r.usage, total_tokens: r.usage.prompt_tokens + r.usage.completion_tokens },
        });
      } catch (err) {
        console.error(`[${id}] lỗi:`, err?.message || err);
        const msg = String(err?.message || err);
        const code = /not logged in|login/i.test(msg) ? 401 : /timeout/i.test(msg) ? 504 : 500;
        return openaiError(res, code, msg, code === 401 ? "authentication_error" : "server_error");
      }
    }

    openaiError(res, 404, `Không có route ${req.method} ${p}`);
  })
  .listen(PORT, () => {
    console.log(`OpenAI-compatible server (Claude headless) tại http://localhost:${PORT}/v1`);
    console.log(`model mặc định ${DEFAULT_MODEL} · effort ${EFFORT} · system ${SYSTEM_MODE} · tools [${TOOLS.join(",")}] · concurrency ${CONCURRENCY}`);
  });
