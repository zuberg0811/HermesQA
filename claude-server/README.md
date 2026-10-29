# claude-server — Claude Code headless làm LLM backend cho hermesQA

Endpoint **OpenAI-compatible** `POST /v1/chat/completions` (Node 22). Bên trong gọi Claude qua
Claude Agent SDK (SDK kèm sẵn binary Claude Code), dùng tài khoản đã `claude login` trên máy,
không cần API key. hermesQA gọi nó qua `OpenAICompatBackend`, không sửa code Python.

## Chạy

```powershell
claude login                       # một lần, nếu chưa đăng nhập
cd claude-server
npm install
npm start                          # http://localhost:3100/v1
```

Cửa sổ khác, chạy hermesQA với profile `claudecode` (đã có trong `.env.example`):

```powershell
$env:PYTHONIOENCODING = "utf-8"
.venv\Scripts\python.exe -m app.cli --repo eval/dataset-repo --base main --head case/cmd-shell-true --skip-static --profile claudecode --out report.md
```

Hoặc đặt làm mặc định trong `.env`:
`AGENT_BACKEND=openai_compat`, `LLM_BASE_URL=http://localhost:3100/v1`, `LLM_API_KEY=none`, `LLM_MODEL=claude-opus-5-5`.

## Mỗi request làm gì

1. `messages[system]` (SKILL.md của vai) được nối vào **preset** system prompt của Claude Code
   (`systemPrompt: {type: "preset", preset: "claude_code", append}`); `SYSTEM_MODE=custom` để chỉ dùng SKILL.md.
2. `messages[user]` thành prompt. Không tool, không MCP, không CLAUDE.md của máy, không lưu transcript.
3. Text trả về được làm sạch: bỏ ```` ```json ````, trích đúng JSON object khi prompt yêu cầu JSON.
4. Trả đúng khung OpenAI: `choices[0].message.content`, `usage.prompt_tokens / completion_tokens`.

## Endpoint

| Method | Path | Ghi chú |
|---|---|---|
| POST | `/v1/chat/completions` | không hỗ trợ `stream=true` |
| GET | `/v1/models` | danh sách model cho phép |
| GET | `/health` | `{ok, running, waiting}` |

## Biến môi trường

Xem `.env.example`: `OPENAI_PORT`, `CLAUDE_MODEL`, `CLAUDE_EFFORT`, `SYSTEM_MODE`, `CONCURRENCY`, `CLAUDE_TOOLS`, `JSON_ONLY`, `ALLOWED_MODELS`.

## Lưu ý

- Đăng nhập claude.ai chỉ dùng cho chính bạn. Nếu deploy cho người khác dùng, đặt `ANTHROPIC_API_KEY`.
- Lỗi `Not logged in` (HTTP 401) nghĩa là máy chưa `claude login` hoặc server chạy dưới user khác.
