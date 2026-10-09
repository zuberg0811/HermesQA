# Website demo HermesQA trên Firebase — hướng dẫn cài đặt

Tài liệu này đi kèm kế hoạch ở mục 6 của `docs/BAO_CAO_TUAN_2026-10-07.md`. Phần dashboard nội bộ chạy trên FastAPI
(`/dashboard`) được mô tả trong `docs/WEB_DEMO.md`; tài liệu này chỉ nói về phần công khai: trang tĩnh trên
Firebase Hosting, đăng nhập GitHub qua Firebase Auth, dữ liệu realtime từ Firestore, và nút "Review thử".

## 1. Kiến trúc và các file liên quan

```
Firebase Hosting (web/public) ──đọc realtime──► Firestore ◄──ghi (Admin SDK)── máy chủ HermesQA
   │  Firebase Auth (GitHub)        hermesqa_runs      ◄── app/runlog.py   (mỗi lần review: CLI, eval, worker)
   │                                hermesqa_eval      ◄── tools/publish_eval.py (bảng benchmark)
   └── nút "Review thử" ──POST──►   hermesqa_demo_jobs ◄── app/demo.py    (job + tiến độ đọc từ log của app.cli)
                        /demo/review (FastAPI, có CORS)
```

| File | Vai trò |
|---|---|
| `web/public/index.html`, `app.js`, `style.css` | Trang web, không cần bước build (ES module + CDN Firebase 10.x) |
| `web/public/firebase-config.example.js` | Mẫu cấu hình; sao chép thành `firebase-config.js` |
| `firebase.json`, `.firebaserc.example` | Cấu hình Hosting + Firestore cho Firebase CLI |
| `firestore.rules`, `firestore.indexes.json` | Quyền: web chỉ đọc 3 collection trên; mọi thứ khác đóng |
| `app/demo.py` | Endpoint `/demo/repos`, `/demo/review`, `/demo/jobs/{id}` và ba hàng rào bảo vệ hạn mức LLM |
| `tools/publish_eval.py` | Đẩy bảng điểm eval lên Firestore |

Lý do Firestore chỉ nhận "bản để hiển thị": worker và sandbox Docker không chạy được trên Cloud Functions, nên phần review
vẫn ở một máy chủ riêng; Postgres vẫn là nơi lưu lịch sử bot và dấu vân tay comment (`app/memory.py`).

## 2. Tạo project Firebase (một lần, ~15 phút)

1. Vào <https://console.firebase.google.com> → *Add project* (ví dụ `hermesqa-demo`). Tắt Google Analytics cho gọn.
2. **Firestore Database** → *Create database* → chế độ *production* (rules sẽ deploy từ repo), chọn region gần (asia-southeast1).
3. **Authentication** → *Sign-in method* → bật **GitHub**. Firebase hiện một *callback URL* dạng
   `https://hermesqa-demo.firebaseapp.com/__/auth/handler`.
4. Trên GitHub: *Settings → Developer settings → OAuth Apps → New OAuth App*: Homepage = `https://hermesqa-demo.web.app`,
   Authorization callback URL = URL ở bước 3. Lấy *Client ID* và tạo *Client secret*, dán ngược vào Firebase rồi *Save*.
5. **Project settings → Your apps → Web app** (biểu tượng `</>`): đặt tên, không cần Hosting ở bước này. Sao chép khối
   `firebaseConfig` vào `web/public/firebase-config.js` (xem file `.example`).
6. **Project settings → Service accounts → Generate new private key**: tải file JSON, để vào `secrets/firebase-sa.json`
   (thư mục `secrets/` đã nằm trong `.gitignore`). Đây là bí mật duy nhất của phần Firebase — key web ở bước 5 không phải bí mật.

## 3. Cấu hình máy chủ HermesQA

Thêm vào `.env`:

```
# run log + job demo ghi vào Firestore (bỏ trống -> ghi file JSON cục bộ trong data/)
FIREBASE_CREDENTIALS=secrets/firebase-sa.json
RUNLOG_BACKEND=auto

# nút "Review thử"
DEMO_REPOS=dataset=eval/dataset-repo,dataset-multi=eval/dataset-repo-multi
DEMO_MAX_PER_HOUR=3
DEMO_REQUIRE_AUTH=true
DEMO_SKIP_STATIC=true          # false nếu máy chủ có Docker và image hermesqa-runner
DEMO_TIMEOUT=900
DEMO_CORS_ORIGINS=https://hermesqa-demo.web.app,https://hermesqa-demo.firebaseapp.com
```

`DEMO_REPOS` là danh sách `tên=đường dẫn cục bộ`; khách chỉ được chọn trong danh sách này và chỉ được chọn nhánh có sẵn,
không bao giờ nhập URL. Hai repo dataset của eval là lựa chọn tự nhiên vì mỗi nhánh là một PR có lỗi biết trước
(tạo bằng `python eval/make_dataset.py` nếu máy chủ chưa có).

Endpoint demo đã được gắn vào API trong `app/main.py` qua `install_demo(app)` (thêm CORS cho origin Firebase và router
`/demo/*`). Khi phát triển có thể chạy riêng không cần Redis/Postgres: `python -m app.demo` (cổng 8010, `DEMO_PORT` để đổi).

Kiểm tra nhanh:

```
curl http://localhost:8000/demo/repos
curl -X POST http://localhost:8000/demo/review -H "Content-Type: application/json" \
     -d "{\"repo\":\"dataset\",\"head\":\"sqli-concat\"}"      # 401 nếu DEMO_REQUIRE_AUTH=true: đúng ý
```

Đặt tạm `DEMO_REQUIRE_AUTH=false` để thử không cần đăng nhập (giới hạn theo IP); nhớ bật lại trước khi mở public.

## 4. Deploy trang web và rules

```
npm install -g firebase-tools        # đã có: firebase --version
firebase login
cp .firebaserc.example .firebaserc   # sửa "hermesqa-demo" thành project id thật
cp web/public/firebase-config.example.js web/public/firebase-config.js   # điền config + apiBase
firebase deploy --only firestore:rules,hosting
```

`apiBase` trong `firebase-config.js` là địa chỉ công khai của FastAPI (VPS/Cloud Run, bắt buộc HTTPS vì trang web chạy
trên HTTPS). Khi thử cục bộ: `apiBase: "http://localhost:8000"` và mở trang bằng `firebase serve` hoặc
`python -m http.server -d web/public 5000`; nhớ thêm `http://localhost:5000` vào `DEMO_CORS_ORIGINS`.

## 5. Đẩy bảng benchmark

```
python tools/publish_eval.py --only out-v3-rescored,out-multi-v3-rescored --note "v3 chốt 07/10/2026, gemini-3.1-flash-lite"
python tools/publish_eval.py --local      # chỉ xem bản tổng hợp trong data/eval_published/
```

Mỗi thư mục `eval/out*` thành một document `hermesqa_eval/<tên>`: điểm theo cấu hình, theo loại lỗi, theo agent
(precision của SE/QA/DevOps và từng công cụ tĩnh), 40 lỗi bỏ sót đầu tiên mỗi cấu hình.

## 6. Tiêu chí "xong" theo kế hoạch tuần

| Bước | Kiểm tra |
|---|---|
| 1 | Mở trang, bấm *Đăng nhập GitHub*, thấy tên + ảnh ở góc phải |
| 2 | Chạy `python -m app.cli ... --skip-static` trên một case; tab *Lần chạy* hiện lần chạy mới với các bước và bảng theo vai |
| 3 | Mở hai tab trình duyệt, chạy review ở tab này, tab kia tự đổi (không bấm tải lại) |
| 4 | Gọi `/demo/review` lần thứ 4 trong một giờ → 429; repo ngoài `DEMO_REPOS` → 404; gọi khi đang chạy → 409 |
| 5 | Worker trên VPS/Cloud Run: mở PR trên repo mẫu → comment xuất hiện trên GitHub và lần chạy xuất hiện trên web |

Bước 1–4 chạy được ngay trên máy phát triển; bước 5 cần credential GitHub App.

## 7. Giới hạn và rủi ro đã tính

- **Quyền ghi**: rules từ chối mọi ghi từ client; chỉ Admin SDK (service account) ghi được. Không đưa file service account
  lên git hay lên Hosting.
- **Hạn mức LLM** là nút cổ chai: ba hàng rào trong `app/demo.py` (allowlist, 3 lần/giờ/người, một lúc một review). Bộ đếm
  tần suất nằm trong bộ nhớ tiến trình: khởi động lại API là đếm lại từ đầu, và nếu chạy nhiều worker uvicorn thì mỗi worker
  đếm riêng — đủ cho demo, không đủ cho sản phẩm.
- **Giới hạn Firestore** 1 MiB/document: run log cắt bớt findings khi quá dài (có cờ `findings_truncated`); job demo giữ
  tối đa 100 finding và 60 000 ký tự báo cáo.
- **Gói miễn phí** (Spark): 50 000 lượt đọc/ngày, 20 000 lượt ghi/ngày, 1 GiB lưu trữ; Hosting 10 GB/tháng. Một lần review
  ghi khoảng 10–20 lần (tiến độ được dồn lại, tối đa một lần ghi mỗi giây), một người xem trang đọc 60 document đầu tiên
  rồi chỉ nhận thay đổi.
- **Phương án dự phòng** nếu Firebase gây rắc rối: FastAPI phục vụ luôn `web/public` và đẩy tiến độ bằng cách hỏi
  `/demo/jobs/{id}` mỗi 2 giây — trang web đã tự chuyển sang chế độ này khi không có `firebase-config.js` (mất đăng nhập
  và lịch sử realtime, nhưng nút "Review thử" vẫn chạy).
