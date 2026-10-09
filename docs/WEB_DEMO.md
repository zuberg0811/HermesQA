# Trang web demo HermesQA (dashboard)

Bản đầu tiên: **đọc dữ liệu đã có** và hiển thị — không đụng vào pipeline review. Ba tab:

| Tab | Đường dẫn | Nội dung |
|---|---|---|
| Tổng quan | `/dashboard` | F1/Precision/Recall hiện tại trên từng bộ eval (`eval/out*`), bảng mọi cấu hình, 10 lần chạy gần nhất |
| Agent | `/dashboard/agents` | Ba vai AI làm gì; **điểm theo agent trên bộ eval** (precision của SE/QA/DevOps và từng công cụ static); **chỉ số theo vai trên mọi lần chạy** (nhận xét thô → giữ → vào báo cáo, tỷ lệ bị kiểm chứng loại, token) |
| Lần chạy | `/dashboard/runs`, `/dashboard/runs/<id>` | Nhật ký từng lần review: từng vai gọi bao nhiêu lượt, sinh/giữ/loại bao nhiêu, token; danh sách nhận xét theo agent; nhận xét bị loại kèm lý do; file bị bỏ qua |

API JSON đi kèm: `/dashboard/api/eval`, `/dashboard/api/runs`, `/dashboard/api/runs/<id>` — để trang tĩnh trên Firebase Hosting (hoặc bất kỳ front-end nào) dùng lại. Phần site tĩnh + Auth GitHub + nút "review thử" nằm ở `docs/FIREBASE_DEMO.md`.

## Chạy cục bộ

```bash
# .env cần GITHUB_APP_ID, GITHUB_WEBHOOK_SECRET (giá trị bất kỳ khi chỉ xem dashboard) và LLM_* như bình thường
.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
# mở http://localhost:8000/dashboard
```

Dashboard không cần Redis/Postgres để hiển thị (chỉ webhook mới cần). Dữ liệu đến từ:

1. `eval/out*/` + `eval/ground_truth*.json` — chấm lại tại chỗ bằng đúng hàm của `eval/score.py` (`score_dir`), nên số trên web luôn khớp `results.md`.
2. **Run log** — mỗi lần chạy `python -m app.cli ...`, `eval/run_eval.py` hay worker GitHub đều ghi một document (xem dưới). Tắt bằng `--no-runlog` (CLI) hoặc `RUNLOG_BACKEND=off`.

## Run log: lưu ở đâu

| `RUNLOG_BACKEND` | Hành vi |
|---|---|
| `auto` (mặc định) | Firestore nếu đặt `FIREBASE_CREDENTIALS`, không thì file JSON cục bộ |
| `local` | `RUNLOG_DIR` (mặc định `data/runs/`, đã gitignore), mỗi lần chạy một file `<thời điểm>_<run_id>.json` |
| `firestore` | collection `FIRESTORE_COLLECTION` (mặc định `hermesqa_runs`); nếu credentials hỏng thì **tự rơi về local và cảnh báo** — review không bao giờ thất bại vì log |
| `off` | không ghi |

Đặt `HERMESQA_RUN_ID=<id>` để ép run_id (job demo trên web và run log dùng chung id). Document được tạo ngay lúc bắt đầu với `status: "running"` và cập nhật `steps`/`current` theo từng bước (static → từng vai → hậu xử lý), kết thúc `status: "done"` hoặc `"failed"`.

### Cài Firebase (Firestore) cho run log

1. Tạo project tại console.firebase.google.com → **Firestore Database** → Create database, chế độ *Native*, vị trí gần (asia-southeast1).
2. Project settings → Service accounts → **Generate new private key** → tải file JSON, đặt ngoài repo (ví dụ `secrets/firebase-sa.json`).
3. Trong `.env`:
   ```
   RUNLOG_BACKEND=firestore
   FIREBASE_CREDENTIALS=secrets/firebase-sa.json
   FIRESTORE_COLLECTION=hermesqa_runs
   ```
4. Chạy một lần CLI; mở Firestore console sẽ thấy document `hermesqa_runs/<run_id>`.
5. Security rules cho trang public chỉ đọc: cho phép `read` collection `hermesqa_runs`, cấm `write` từ client (worker ghi bằng service account nên không qua rules). File `firestore.rules` ở gốc repo.

Thư viện: `firebase-admin>=6.5` (trong `requirements.txt`). Lưu ý phiên bản: firebase-admin 7.x ghim `httpx==0.28.1`, nên `openai` phải ≥ 1.58 (bản 1.51 vỡ với httpx 0.28) — `requirements.txt` đã ghim đúng, đừng cài lẻ.

### Cấu trúc một document run log

```jsonc
{
  "run_id": "3f9a1c2b7d4e", "started_at": "2026-10-09T03:40:12+00:00",   // UTC, ISO-8601
  "status": "done",                  // running | done | failed
  "steps": [{"t": "2026-10-09T03:40:13+00:00", "step": "static", "detail": "3 finding"}, ...],
  "current": "done",
  "source": "cli",                   // cli | eval | worker
  "repo": "owner/repo", "pr_number": 0, "title": "main..HEAD", "head_sha": "...",
  "model": "gemini-3.1-flash-lite",
  "config": {"verify_mode": "factcheck", "skip_static": false, "roles": ["SE", "QA"]},
  "seconds": 18.5,
  "stats": {"llm_calls": 4, "verify_calls": 2, "raw_findings": 5, "removed_by_factcheck": 0,
            "tokens_in": 6100, "tokens_out": 250, "anchor_code": 5},
  "role_stats": {"SE": {"calls": 1, "raw": 3, "kept": 3, "removed": 0, "final": 3,
                        "tokens_in": 3200, "tokens_out": 140, "verify_calls": 1},
                 "QA": {"...": "..."}},
  "static_by_tool": {"semgrep": 2, "bandit": 1},
  "failed_roles": [], "summaries": {"SE": "...", "QA": "..."},
  "findings": [ /* Finding.model_dump(): file, line, severity, category, title, explanation, suggested_fix,
                   confidence, source, role, existing_code, anchor, fingerprint */ ],
  "removed": [ {"role": "QA", "file": "...", "line": 7, "title": "...", "ground": "B", "evidence": "...", "reason": "...", "check": "..."} ],
  "skipped": [ {"path": ".env", "reason": "secret", "detail": ""} ],
  "conclusion": null,                // worker: success | failure
  "counts": {"static": 3, "llm_raw": 5, "llm_kept": 5, "final": 7,
             "by_severity": {"high": 1, "medium": 6}, "by_category": {"bug": 4, "security": 3}},
  "eval_case": "eq-none"             // chỉ khi source = eval
}
```

Firestore giới hạn 1 MiB/document: `findings` bị cắt ở 300 phần tử (có cờ `findings_truncated`).

## "Chấm điểm từng agent" nghĩa là gì trên trang này

- **Có đáp án (bộ eval):** precision của agent = nhận xét của agent đó được khớp đáp án / tổng nhận xét của agent đó. Recall không tính theo agent vì một lỗi không "thuộc" riêng vai nào (SE và QA có thể cùng bắt).
- **Không cần đáp án (mọi lần chạy):** nhận xét thô mỗi lần, tỷ lệ bị bước kiểm tra sự thật loại, số vào báo cáo sau gộp trùng và ngưỡng lọc, token mỗi lần, số lần vai lỗi. Đây là số để so sánh prompt/model qua thời gian.
