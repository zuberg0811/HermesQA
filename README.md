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

## 4b. Học từ phản hồi trên PR (repo memory)
Mỗi comment inline của bot mang một dấu ẩn `<!-- hermesqa:fp=… -->` nối nó với finding. Người review có hai cách dạy bot:
- **Trả lời vào comment** ("không phải lỗi", "bỏ qua", "cố ý", hoặc "đúng rồi", "đã sửa"…): GitHub gửi `pull_request_review_comment`, webhook đẩy job `app.worker.ingest_feedback`, `app/feedback.py` phân loại reject/accept và ghi một ghi chú vào bảng `repo_memory`.
- **Bấm 👍/👎**: GitHub không có webhook cho reaction, nên ở lần push tiếp theo worker đọc lại reactions của các comment cũ (`feedback.sync_reactions`) trước khi gọi LLM.

Ghi chú (ví dụ ``KHÔNG báo lại: `scripts/run.py` — [style] Dùng print() thay logging. Người review nói đây không phải vấn đề: "scripts dùng print là chuẩn"``) được nạp vào prompt của mọi vai ở các lần review sau của cùng repo (`memory.get_repo_memory`). Ghi chú về lỗi bảo mật chỉ mang tính tham khảo, bot vẫn báo nếu gặp mẫu đó ở chỗ khác. Mỗi phản hồi thô được lưu ở bảng `feedback` và chỉ ghi nhận một lần. GitHub App cần subscribe thêm event **Pull request review comment**.

**Hiệu chỉnh confidence theo 👎** (`app/review/calibrate.py`, không cần LLM): mỗi finding được gán khoá `<nguồn>:<luật> | <thư mục cấp 1>` (static tool dùng mã luật, LLM dùng category). Nếu khoá đó đã bị 👎 ít nhất `FEEDBACK_MIN_REJECTS` lần (mặc định 2) và chiếm từ `FEEDBACK_REJECT_RATIO` (0.67) tổng phản hồi, confidence nhân `FEEDBACK_CONFIDENCE_FACTOR` (0.5) trước khi qua ngưỡng `MIN_CONFIDENCE`, nên nhận xét loại đó rơi khỏi comment inline mà không phụ thuộc model có "nghe" ghi chú hay không; lý do được ghi vào phần giải thích. Nhóm bảo mật / bug mức high trở lên không bao giờ bị hạ.

**Agent tự ghi chú sau mỗi lần review** (`app/review/reflect.py`, giống "agent-curated memory" của Hermes Agent): sau khi đã đăng review, thêm một lần gọi LLM nhìn lại lần chạy — nhận xét nào được giữ, nhận xét nào bị fact-check loại và vì sao, ghi chú nào đã có — rồi rút ra 0–3 ghi chú về repo (convention / false_positive / pattern), ghi vào `repo_memory` nguồn `agent`. Trong prompt lần sau các ghi chú này mang tiền tố "(tự rút ra)" để phân biệt với ghi chú do người dạy. Hàng rào: lọc trùng, không nhận ghi chú "không báo" về bảo mật, trần `AGENT_MEMORY_MAX_NOTES` (30) ghi chú agent mỗi repo (ghi chú từ người không bị cắt), lỗi ở bước này không làm hỏng review. Tắt bằng `AGENT_MEMORY_ENABLED=false`.

**Memory chọn theo lô file** (`memory.select_memory`): mỗi lần gọi LLM chỉ nhận ghi chú liên quan tới các file trong lô — gắn đúng file trước, cùng thư mục, rồi ghi chú chung của repo; cùng hạng thì ghi chú do người dạy trước, category hợp vai trước, rồi mới nhất. Ghi chú về file khác chỉ được lấy khi còn chỗ (tối đa 20 dòng/lô).


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

## 7. Website demo (dashboard + Firebase)
- Dashboard nội bộ tại `/dashboard` của API (điểm theo agent, nhật ký từng lần chạy): xem [`docs/WEB_DEMO.md`](docs/WEB_DEMO.md).
- Trang công khai trên Firebase Hosting (đăng nhập GitHub, lịch sử realtime từ Firestore, nút "Review thử" gọi `/demo/review`): xem [`docs/FIREBASE_DEMO.md`](docs/FIREBASE_DEMO.md). Mã ở `web/public/`, `app/demo.py`, `firestore.rules`; đẩy bảng benchmark bằng `python tools/publish_eval.py`.

## Bảo mật
- Code PR **không bao giờ được thực thi**; chỉ static tools chạy trong container `--network none`, `cap_drop ALL`, giới hạn CPU/RAM, mount source read-only.
- Token GitHub là installation token ngắn hạn, bị gỡ khỏi `.git` ngay sau khi clone.
- Webhook xác thực HMAC SHA-256.

## Roadmap
GitLab/Bitbucket · suggestion blocks · agent tự ghi chú sau mỗi lần chạy · lọc memory theo file/vai · hiệu chỉnh confidence theo tỉ lệ 👎 · multi-tenant + billing.
