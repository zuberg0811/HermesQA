# HermesQA — AI Auto-QA & Code Reviewer (Micro-SaaS)

Bot tự động review pull request theo 3 vai **SE / QA / DevOps**, kết hợp static analysis (semgrep, bandit, gitleaks, hadolint, ruff) chạy trong sandbox Docker không có network + LLM/Hermes Agent, rồi comment inline lên PR, cập nhật GitHub Checks và sinh report Markdown.

```
GitHub webhook ─▶ FastAPI (api) ─▶ Redis queue ─▶ Worker
                                                   ├─ clone PR head (depth 1)
                                                   ├─ sandbox: static tools (--network none)
                                                   ├─ router: chọn vai theo file thay đổi
                                                   ├─ Agent x vai (SKILL.md làm system prompt) + self-verify
                                                   ├─ dedupe / confidence gate / max comments
                                                   └─ PR review + Checks API + report .md + Postgres
```

## Cấu trúc
```
app/
  main.py            webhook FastAPI (HMAC verify, enqueue)
  worker.py          pipeline review 1 PR
  cli.py             chạy offline trên repo local (dev + eval)
  github_app.py      GitHub App auth, diff, review comments, Checks API
  sandbox.py         chạy runner container an toàn
  memory.py          lưu run + repo memory (Postgres)
  review/
    agent.py         backend LLM (OpenAI-compatible) hoặc Hermes CLI, self-verify
    diff_utils.py    parse diff, map dòng comment, lấy context
    router.py        chọn vai
    static_tools.py  chuẩn hoá output semgrep/gitleaks/hadolint/ruff
    postprocess.py   dedupe, policy, format comment, conclusion
    report.py        Markdown report + PR summary
    schemas.py       Finding / AgentOutput / ReviewConfig
  skills/{SE,QA,DevOps}/SKILL.md   bộ prompt chuyên sâu theo vai
runner/              image sandbox (Dockerfile + run_static.sh)
```

## 1. Chạy nhanh (offline, không cần GitHub App)
```bash
python -m venv .venv && source .venv/bin/activate   # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env            # điền LLM_API_KEY (OpenRouter/OpenAI) hoặc trỏ LLM_BASE_URL về vLLM local
docker build -t hermesqa-runner:latest runner/
export WORKDIR=/tmp/hqa REPORT_DIR=/tmp/hqa-reports

# review diff giữa 2 branch của một repo bất kỳ
python -m app.cli --repo ../my-project --base main --head feature-x --out report.md --json findings.json
# không có docker: --skip-static
```

## 2. Chạy full với GitHub App
1. Tạo GitHub App: **Settings → Developer settings → GitHub Apps → New**
   - Webhook URL: `https://<your-host>/webhook/github`, secret = `GITHUB_WEBHOOK_SECRET`
   - Permissions: Pull requests **Read & write**, Checks **Read & write**, Contents **Read**, Metadata **Read**
   - Subscribe events: **Pull request**
   - Generate private key → lưu vào `secrets/github-app.pem`
2. Điền `GITHUB_APP_ID`, secret vào `.env`.
3. Dev local dùng tunnel: `ngrok http 8000` hoặc `smee.io`.
4. Chạy:
```bash
docker build -t hermesqa-runner:latest runner/
docker compose up --build
```
5. Install App vào repo, mở PR → bot comment sau ~1–3 phút.

## 3. Dùng Hermes Agent thay LLM trực tiếp
Đặt `AGENT_BACKEND=hermes_cli` và chỉnh `HERMES_CMD` thành lệnh của bản Hermes bạn cài (prompt qua stdin, JSON ra stdout). Skill của từng vai nằm ở `app/skills/<Role>/SKILL.md` theo định dạng skill Markdown (frontmatter `name/description`) nên có thể copy thẳng vào thư mục skills của Hermes. Nếu bản Hermes của bạn không có CLI phù hợp, viết adapter mới trong `app/review/agent.py` theo `AgentBackend` protocol (một hàm `run(role, prompt) -> AgentOutput`).

## 4. Cấu hình theo repo
Copy `.hermesqa.yml.example` → `.hermesqa.yml` ở gốc repo được review: chọn vai, ignore glob, convention, max comments, có block merge khi critical hay không.

## 5. Test
```bash
pip install pytest && pytest -q
```

## 6. Eval cho đồ án
Bộ eval hoàn chỉnh nằm trong [`eval/`](eval/README.md): 35 PR có lỗi biết trước (security/devops/bug/test/style) + ground truth tự sinh + script chấm precision/recall/F1, thời gian và token/PR cho 3 cấu hình.
```bash
python eval/make_dataset.py                 # sinh dataset + ground_truth.json
python eval/run_eval.py --configs static    # rồi: --configs llm,both (cần LLM_API_KEY)
python eval/score.py                        # -> eval/out/results.md
```

## Bảo mật
- Code PR **không bao giờ được thực thi**; chỉ static tools chạy trong container `--network none`, `cap_drop ALL`, giới hạn CPU/RAM, mount source read-only.
- Token GitHub là installation token ngắn hạn, bị gỡ khỏi `.git` ngay sau khi clone.
- Webhook xác thực HMAC SHA-256.

## Roadmap
GitLab/Bitbucket · suggestion blocks · dashboard web · feedback 👍/👎 → repo memory tự động · multi-tenant + billing.
