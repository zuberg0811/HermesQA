# Báo cáo tiến độ đồ án: HermesQA — Bot AI tự động review Pull Request

**Cập nhật:** 18/09/2026 · **Trạng thái:** Hệ thống chạy được đầu-cuối, đang chạy thực nghiệm đánh giá

---

## 1. Bài toán và mục tiêu

### 1.1 Vấn đề
Code review thủ công là nút thắt trong quy trình phát triển phần mềm: tốn thời gian của lập trình viên giàu kinh nghiệm, chất lượng không đồng đều giữa các reviewer, và người review dễ bỏ sót lỗi bảo mật khi đang tập trung vào logic nghiệp vụ.

Hai hướng tự động hóa hiện có đều không trọn vẹn:
- **Static analysis** (semgrep, SonarQube…): chính xác, rẻ, nhưng chỉ bắt được các mẫu lỗi đã định nghĩa sẵn; không hiểu ngữ cảnh nghiệp vụ, không biết "thay đổi này thiếu test cho trường hợp nào".
- **LLM review thuần túy**: hiểu ngữ cảnh, nhận xét được về thiết kế và test, nhưng dễ *hallucinate* (bịa lỗi không tồn tại, báo sai số dòng) và tốn chi phí token.

### 1.2 Mục tiêu đồ án
Xây dựng hệ thống kết hợp cả hai, với ba đóng góp chính:

1. **Kiến trúc đa vai trò (multi-role)**: mô phỏng quy trình review thật với ba vai **SE** (Software Engineer — logic, thiết kế), **QA** (test coverage, edge case), **DevOps** (Dockerfile, CI, hạ tầng). Mỗi vai có một *skill prompt* chuyên sâu riêng.
2. **Sandbox an toàn**: code trong PR đến từ nguồn không tin cậy, nên static tools chạy trong container Docker cô lập hoàn toàn (không có mạng, bỏ mọi capability, source mount chỉ đọc). **Code của PR không bao giờ được thực thi.**
3. **Cơ chế chống hallucination**: bước *self-verify* (LLM tự kiểm chứng lại phát hiện của chính mình), kết hợp lọc theo confidence, dedupe với kết quả static tool, và ràng buộc số dòng phải nằm trong diff.

---

## 2. Kiến trúc hệ thống

```
GitHub webhook ─▶ FastAPI (api) ─▶ Redis queue ─▶ Worker
                                                   ├─ clone PR head (depth 1)
                                                   ├─ sandbox: static tools (--network none)
                                                   ├─ router: chọn vai theo file thay đổi
                                                   ├─ Agent × vai (SKILL.md làm system prompt) + self-verify
                                                   ├─ dedupe / confidence gate / max comments
                                                   └─ PR review + Checks API + report .md + Postgres
```

### 2.1 Vì sao tách API và Worker qua hàng đợi?
GitHub yêu cầu webhook phản hồi trong **10 giây**, trong khi một lần review mất 1–3 phút. Vì vậy tầng API chỉ xác thực chữ ký HMAC, đẩy job vào Redis rồi trả `202` ngay lập tức. Worker xử lý bất đồng bộ. Đây là ràng buộc bắt buộc của nền tảng, không phải lựa chọn tối ưu hóa.

### 2.2 Vai trò từng module (tổng ~1.700 dòng Python)

| File | Trách nhiệm |
|---|---|
| `app/main.py` | Webhook FastAPI: xác thực HMAC SHA-256, lọc sự kiện, đẩy job vào queue |
| `app/worker.py` | Pipeline review đầy đủ cho 1 PR (7 bước, xem mục 2.3) |
| `app/cli.py` | Chạy offline trên repo local — dùng cho phát triển và chạy thực nghiệm |
| `app/github_app.py` | Xác thực GitHub App (JWT → installation token), lấy diff, đăng review, Checks API |
| `app/sandbox.py` | Khởi chạy container static analysis với các ràng buộc an toàn |
| `app/memory.py` | Lưu lịch sử review + "bộ nhớ repo" (convention team đã thống nhất) vào Postgres |
| `app/review/agent.py` | Backend LLM (chuẩn OpenAI-compatible) hoặc Hermes CLI; bước self-verify |
| `app/review/diff_utils.py` | Parse unified diff, ánh xạ số dòng để comment inline, trích ngữ cảnh file |
| `app/review/router.py` | Chọn vai nào cần chạy dựa trên loại file thay đổi (tiết kiệm token) |
| `app/review/static_tools.py` | Chuẩn hóa output 5 công cụ về cùng một cấu trúc `Finding` |
| `app/review/postprocess.py` | Dedupe, áp policy, định dạng comment, quyết định kết luận check run |
| `app/review/report.py` | Sinh báo cáo Markdown và phần tóm tắt đăng lên PR |
| `app/skills/{SE,QA,DevOps}/SKILL.md` | Prompt chuyên sâu cho từng vai (checklist, thang severity, quy tắc chống nhiễu) |

### 2.3 Bảy bước của pipeline review
1. **Clone** PR head với `--depth 1`, sau đó **gỡ remote ngay** để token không nằm lại trong `.git`.
2. **Đọc cấu hình** `.hermesqa.yml` của repo (chọn vai, ignore glob, convention, ngưỡng chặn merge).
3. **Static analysis** trong sandbox; chỉ giữ finding thuộc file có trong diff.
4. **Chạy agent** theo từng vai; mỗi vai nhận diff + ngữ cảnh file + tóm tắt kết quả static (để *không lặp lại* lỗi static đã bắt) + bộ nhớ repo.
5. **Self-verify**: LLM đối chiếu lại từng finding với code, loại bỏ finding sai và hiệu chỉnh số dòng.
6. **Post-process**: dedupe, lọc theo confidence/severity, giới hạn số comment.
7. **Đăng kết quả**: comment inline lên PR, cập nhật GitHub Checks, sinh báo cáo Markdown, lưu Postgres.

### 2.4 Bộ công cụ static analysis (5 công cụ)
| Công cụ | Phạm vi |
|---|---|
| **semgrep** | Mẫu lỗi bảo mật/logic đa ngôn ngữ (SQL injection, command injection…) |
| **bandit** | Lỗi bảo mật đặc thù Python (`shell=True`, `eval`, MD5…) |
| **gitleaks** | Secret/API key bị hard-code |
| **hadolint** | Lỗi và anti-pattern trong Dockerfile |
| **ruff** | Style, import thừa, lỗi cú pháp Python |

---

## 3. Thiết kế bảo mật

Đây là phần cần nhấn mạnh khi bảo vệ, vì hệ thống **nhận và xử lý code từ người lạ** (bất kỳ ai mở PR).

| Rủi ro | Biện pháp |
|---|---|
| Code độc hại trong PR được thực thi | **Không bao giờ thực thi code PR.** Chỉ static tools đọc file. |
| Công cụ phân tích bị lợi dụng để gọi ra ngoài | Container chạy với `--network none` — không có mạng |
| Leo thang đặc quyền trong container | `cap_drop: ALL`, `security_opt: no-new-privileges` |
| Cạn kiệt tài nguyên (fork bomb, quét vô hạn) | Giới hạn 2 CPU, 2 GB RAM, `pids_limit=256`, timeout 600s |
| Sửa đổi mã nguồn khi phân tích | Mount `/src` ở chế độ **read-only** |
| Lộ token GitHub | Dùng installation token ngắn hạn; gỡ remote khỏi `.git` ngay sau clone |
| Webhook giả mạo | Xác thực HMAC SHA-256 với `compare_digest` (chống timing attack) |

**Hệ quả thiết kế đáng lưu ý:** vì sandbox không có mạng, bộ rule của semgrep phải được **nạp sẵn vào image lúc build** (`/opt/semgrep-rules`) thay vì tải lúc chạy bằng `--config auto`. Đây là một lỗi thực tế đã gặp và sửa (mục 5).

---

## 4. Cơ chế chống hallucination (đóng góp học thuật chính)

LLM khi review code thường mắc ba lỗi: bịa ra vấn đề không tồn tại, báo sai số dòng, và lặp lại điều static tool đã nói. Hệ thống dùng **bốn lớp phòng vệ xếp chồng**:

1. **Ràng buộc đầu vào**: prompt đưa kèm số dòng thật của từng dòng code, và yêu cầu rõ "chỉ báo dòng nằm trong diff, không bịa dòng".
2. **Self-verify**: kết quả của vòng 1 được đưa lại cho LLM với vai trò "reviewer cấp cao kiểm chứng", yêu cầu loại bỏ finding không thật sự là vấn đề và hiệu chỉnh số dòng lệch.
3. **Ràng buộc kỹ thuật khi map dòng**: `commentable_line()` chỉ chấp nhận dòng nằm trong diff; nếu lệch ≤ 3 dòng thì "snap" về dòng gần nhất, lệch hơn thì finding bị chuyển sang phần phụ lục thay vì comment inline.
4. **Dedupe ưu tiên static tool**: khi LLM và static tool cùng báo một vị trí (lệch ≤ 2 dòng, cùng category), giữ kết quả của static tool vì độ tin cậy cao hơn.

Ngoài ra còn **confidence gate** (mặc định ≥ 0,6) và **giới hạn số comment** (mặc định 15) để tránh làm nhiễu PR.

---

## 5. Các lỗi đã phát hiện và sửa trong quá trình triển khai

Phần này minh chứng cho việc hệ thống đã thực sự được chạy và kiểm thử, không chỉ viết xong để đó.

### Nhóm A — Lỗi khiến chức năng không chạy được

| # | Lỗi | Nguyên nhân gốc | Cách sửa |
|---|---|---|---|
| A1 | **Semgrep chưa bao giờ chạy được** | Sandbox `--network none` nhưng dùng `--config auto` (cần tải rule từ Internet) | Nạp sẵn ruleset vào image lúc build |
| A2 | Semgrep crash ngay khi khởi động | `python:3.12-slim` không còn `pkg_resources`; semgrep 1.90 vẫn phụ thuộc | Ghim `setuptools<81` trong image |
| A3 | Semgrep từ chối toàn bộ thư mục rule | Repo `semgrep-rules` chứa `.pre-commit-config.yaml` không phải file rule | Xóa các YAML không phải rule lúc build |
| A4 | **Bandit được cài nhưng không bao giờ được gọi** | Thiếu lệnh gọi trong script runner và thiếu parser | Bổ sung lệnh gọi + parser chuẩn hóa output |
| A5 | **Bước self-verify chưa bao giờ chạy được** | `VERIFY_PROMPT` ghép thêm `OUTPUT_CONTRACT` (chứa `{}` của JSON schema) rồi gọi `.format()` → Python hiểu nhầm `{"summary"...}` là placeholder → `KeyError`. Lỗi bị `try/except` nuốt mất nên không ai biết. | Ghép `OUTPUT_CONTRACT` **sau** khi format; bổ sung test hồi quy |

> **A5 là lỗi nghiêm trọng nhất**: cơ chế chống hallucination — đóng góp chính của đồ án — thực chất chưa từng hoạt động, và thất bại trong im lặng.

### Nhóm B — Lỗi về tính đúng đắn và độ bền

| # | Lỗi | Hệ quả | Cách sửa |
|---|---|---|---|
| B1 | Worker mount `/data` bằng named volume | Khi triển khai thật, sandbox sẽ nhận thư mục rỗng (worker truyền đường dẫn cho Docker daemon của **host**, không phải của chính nó) | Chuyển sang bind mount `/data:/data` |
| B2 | `--roles ""` (chế độ chỉ static) không hoạt động | `"".split(",")` trả về `[""]` chứ không phải `[]` | Lọc bỏ phần tử rỗng |
| B3 | Ghi file JSON lỗi trên Windows | Thiếu `encoding="utf-8"`, mặc định cp1252 không mã hóa được tiếng Việt | Chỉ định encoding tường minh |
| B4 | Một vai lỗi làm hỏng cả lần review | `cli.py` không bắt exception theo vai như `worker.py` | Bắt lỗi từng vai, vẫn xuất báo cáo phần còn lại, trả exit code 2 |
| B5 | Gặp lỗi 429/503 của LLM là hỏng luôn | Không có retry | Bật `max_retries=5` (backoff lũy thừa) |

### Nhóm C — Tối ưu hiệu năng
**Vấn đề:** mỗi lần quét static mất ~350 giây. Hai nguyên nhân:
- Sandbox quét **toàn bộ repo**, trong khi tầng trên chỉ giữ finding ở file có trong diff → phần lớn công sức bị vứt bỏ.
- Semgrep nạp cả **2.150 rule** của mọi ngôn ngữ, kể cả khi PR chỉ sửa một file Python.

**Giải pháp:** chỉ quét file trong diff, và chỉ nạp rule của ngôn ngữ xuất hiện trong diff.

**Kết quả đo được:**

| Cấu hình | Thời gian |
|---|---|
| Toàn bộ 2.150 rule (ban đầu) | 349 s |
| Chỉ rule Python | 79 s |
| Python + generic (đang dùng) | 117 s |
| Dockerfile + generic (đang dùng) | 71 s |

→ **Nhanh gấp ~3 lần, kết quả phát hiện không đổi** (đã kiểm chứng bằng cách so sánh finding trước/sau).

---

## 6. Thiết kế thực nghiệm đánh giá

### 6.1 Bộ dữ liệu
Vì không có bộ dữ liệu chuẩn cho bài toán này, đồ án **tự sinh dataset có kiểm soát**: một repo Python mẫu (nhánh `main` sạch) và **35 nhánh**, mỗi nhánh tiêm đúng **một lỗi đã biết trước**.

| Loại lỗi | Số lượng | Ví dụ |
|---|---|---|
| Security | 13 | SQL injection (nối chuỗi và f-string), command injection, secret hard-code, `eval()`, `yaml.load` không an toàn, `pickle.loads`, MD5 cho mật khẩu, tắt kiểm tra TLS, Flask debug mode |
| DevOps | 8 | Base image `latest`, chạy bằng root, `COPY . .`, `ADD` thay `COPY`, `sudo` trong Dockerfile, dependency không ghim version |
| Bug | 7 | Mutable default argument, bare `except`, off-by-one, `== None`, chia cho 0, điều kiện luôn đúng, biến chưa định nghĩa |
| Test/QA | 4 | Skip test không lý do, assertion yếu, hàm mới không có test, test mất assertion |
| Style/Performance | 4 | Nối chuỗi trong vòng lặp, import thừa, `print()` thay logging, trùng lặp logic |
| **Tổng** | **35 case / 36 lỗi** | |

**Điểm mạnh về phương pháp:** ground truth (file, số dòng, loại lỗi) được **tính tự động** từ nội dung file thay vì gõ tay, nên số dòng luôn chính xác kể cả khi nội dung file thay đổi.

### 6.2 Ba cấu hình so sánh
| Cấu hình | Mô tả | Mục đích |
|---|---|---|
| `static` | Chỉ 5 công cụ static | Đường cơ sở (baseline) |
| `llm` | Chỉ LLM 3 vai | Đo năng lực riêng của LLM |
| `both` | Kết hợp (mặc định của sản phẩm) | Kiểm chứng giả thuyết "kết hợp tốt hơn từng phần" |

### 6.3 Tiêu chí chấm điểm
Một phát hiện được tính **đúng (True Positive)** khi khớp với một lỗi ground truth chưa được khớp trước đó, thỏa đồng thời:
- cùng file,
- lệch dòng **≤ 3**,
- loại lỗi (category) nằm trong danh sách chấp nhận.

Từ đó tính **Precision = TP/(TP+FP)**, **Recall = TP/(TP+FN)**, **F1**. Ngoài ra đo **thời gian/PR** và **token/PR**.

Mỗi ground truth chỉ được khớp một lần, nên việc báo trùng bị tính là dương tính giả.

**Lưu ý về tính công bằng của phép đo:** khi chạy thực nghiệm, ngưỡng lọc được nới ra (`MIN_SEVERITY=low`, `MIN_CONFIDENCE=0`) vì mục tiêu là đo *khả năng phát hiện*, tách biệt khỏi *chính sách lọc comment* của sản phẩm. Precision báo cáo là **cận dưới**, vì các phát hiện "đúng nhưng nằm ngoài ground truth" (ví dụ LLM chê thiếu docstring) vẫn bị tính là dương tính giả.

---

## 7. Kết quả hiện có

Thực nghiệm đã chạy **đầy đủ 35 case × 3 cấu hình = 105 lượt chạy, không có lượt nào thất bại**.

| Cấu hình | Số case | TP | FP | FN | Precision | Recall | F1 | Thời gian/PR | Token/PR |
|---|---|---|---|---|---|---|---|---|---|
| `static` | 35/35 | 19 | 26 | 17 | 42,2 % | 52,8 % | 46,9 % | 125,6 s | 0 |
| `llm` | 35/35 | 34 | 27 | 2 | **55,7 %** | **94,4 %** | **70,1 %** | **13,3 s** | 7.229 |
| `both` | 35/35 | 34 | 54 | 2 | 38,6 % | 94,4 % | 54,8 % | 144,1 s | 7.461 |

Tổng số lỗi ground truth: 36. Chi tiết từng case: `eval/out/results.md`.

**Khả năng phát hiện theo nhóm lỗi (bắt được / tổng):**

| Nhóm | `static` | `llm` | `both` |
|---|---|---|---|
| security | 12/13 | 12/13 | 12/13 |
| devops | 6/8 | 8/8 | 8/8 |
| bug | 1/7 | 6/7 | 6/7 |
| test | 0/4 | 4/4 | 4/4 |
| style | 0/3 | 3/3 | 3/3 |
| performance | 0/1 | 1/1 | 1/1 |

### 7.1 Nhận định

**1. Giả thuyết "kết hợp tốt hơn từng phần" KHÔNG được số liệu ủng hộ.**
Cấu hình `both` có recall **bằng đúng** `llm` (94,4 %) nhưng precision tụt từ 55,7 % xuống 38,6 %, kéo F1 từ 70,1 % xuống 54,8 %. Kiểm chứng trực tiếp trên tập phát hiện cho thấy **tập lỗi mà static bắt được là tập con thực sự của tập lỗi mà LLM bắt được**: static không đóng góp một phát hiện đúng nào mà LLM bỏ sót (0/19), trong khi thêm vào 27 dương tính giả và khoảng 130 giây mỗi PR. Số FP của `both` (54) xấp xỉ tổng FP của hai cấu hình thành phần (26 + 27), nghĩa là bước khử trùng lặp hiện tại **gần như không gộp được** phát hiện giữa hai nguồn.

**2. LLM vượt static ở mọi nhóm lỗi, không riêng nhóm "khó định nghĩa mẫu".**
Dự đoán ban đầu cho rằng static mạnh ở security/devops còn LLM mạnh ở test/style. Thực tế LLM **bằng hoặc hơn** static ở cả sáu nhóm: ngang ở security (12/13), hơn ở devops (8/8 so với 6/8), và bỏ xa ở bug (6/7 so với 1/7), test (4/4 so với 0/4), style (3/3 so với 0/3), performance (1/1 so với 0/1). Recall tổng của static chỉ đạt 52,8 %.

**3. Static chậm hơn LLM khoảng 10 lần.**
125,6 s/PR so với 13,3 s/PR. Chi phí này đến từ việc khởi tạo container sandbox và chạy tuần tự 5 công cụ, chứ không phải từ khối lượng phân tích.

**4. Precision của cả ba cấu hình đều thấp** (38–56 %). Cần đọc kèm lưu ý ở mục 6.3: ngưỡng lọc được nới hết cỡ (`MIN_SEVERITY=low`, `MIN_CONFIDENCE=0`) và mọi phát hiện nằm ngoài ground truth đều bị tính là dương tính giả, kể cả khi phát hiện đó hợp lệ. Đây là **cận dưới** của precision, không phải precision của sản phẩm khi bật chính sách lọc mặc định.

**5. Hai lỗi không cấu hình nào bắt được:**
- `eq-none` (so sánh `None` bằng `==` thay vì `is`) — lỗi thật, cả ba cấu hình đều bỏ sót.
- `secret-aws` dòng 7 — **thực chất là hạn chế của phép đo, không phải lỗi phát hiện**. Case này có 2 ground truth ở dòng 6 và 7 (access key và secret key), nhưng cả static lẫn LLM đều báo **một** phát hiện duy nhất ở dòng 6 bao trùm cả cặp. Do quy tắc chấm mỗi ground truth chỉ khớp tối đa một phát hiện, dòng 7 bị tính là bỏ sót. Recall thực tế vì vậy **cao hơn** con số báo cáo một chút.

### 7.2 Hệ quả cho thiết kế sản phẩm

Với dataset này, cấu hình mặc định `both` **không phải lựa chọn tốt nhất**: nó đắt hơn, chậm hơn 10 lần và có F1 thấp hơn `llm` thuần. Hai hướng xử lý:
- Nếu ưu tiên F1 và tốc độ: dùng `llm` làm mặc định.
- Nếu vẫn muốn giữ static (vì lý do kiểm toán, hoặc vì tin rằng nó bền vững hơn LLM trên dự án thật): cần **cải thiện bước khử trùng lặp và lọc** để static không bơm thêm FP, thay vì cộng gộp thô như hiện tại.

Cần nhấn mạnh: kết luận này gắn với **dataset lỗi tiêm nhân tạo cỡ 35 case**, nơi lỗi tương đối rõ ràng và nằm trong diff nhỏ — điều kiện thuận lợi cho LLM. Trên codebase thật với diff lớn và ngữ cảnh phức tạp, cán cân có thể khác (xem mục 9).

### Kiểm thử đơn vị
`pytest`: **4/4 test pass** — bao gồm parse diff, chọn vai, dedupe/policy, và test hồi quy cho lỗi A5.

---

## 8. Trạng thái các hạng mục

| Hạng mục | Trạng thái |
|---|---|
| Kiến trúc và toàn bộ mã nguồn | ✅ Hoàn thành (~1.700 dòng) |
| Sandbox static analysis (5 công cụ) | ✅ Đã chạy và kiểm chứng |
| Agent 3 vai + self-verify | ✅ Đã chạy đầu-cuối với Google Gemini |
| CLI chạy offline | ✅ Hoàn thành |
| Bộ thực nghiệm (dataset + chấm điểm) | ✅ Hoàn thành |
| Kiểm thử đơn vị | ✅ 4/4 pass |
| **Chạy thực nghiệm đầy đủ** | ✅ **Hoàn thành — 105/105 lượt chạy thành công** |
| Triển khai GitHub App thật | ⏳ Chưa — cần đăng ký GitHub App và tunnel công khai |
| Viết báo cáo/luận văn | 🔄 Đang viết — đã có đủ số liệu thực nghiệm |

---

## 9. Hạn chế và hướng phát triển

### Hạn chế hiện tại (nên chủ động nêu khi bảo vệ)
1. **Dataset là lỗi tiêm nhân tạo**, không phải PR thật từ dự án mở. Lỗi tiêm thường "sạch" và dễ nhận ra hơn lỗi thực tế, nên kết quả có thể lạc quan hơn thực tế.
2. **Cỡ mẫu 35 case** đủ để so sánh xu hướng nhưng chưa đủ để kết luận có ý nghĩa thống kê.
3. **Chỉ đánh giá trên Python và Dockerfile**; chưa kiểm chứng với JavaScript, Java, Go.
4. **Phụ thuộc vào một nhà cung cấp LLM**; kết quả có thể khác khi đổi model. (Kiến trúc đã tách qua `AgentBackend` protocol nên đổi model không phải sửa logic.)
5. **Bộ nhớ repo chưa có vòng phản hồi tự động**: hiện phải nhập tay, chưa học từ phản ứng 👍/👎 của lập trình viên.
6. **Chưa đo chi phí thực tế bằng tiền** trên quy mô lớn.
7. **Bước khử trùng lặp giữa static và LLM chưa hiệu quả**: số dương tính giả của cấu hình `both` gần bằng tổng của hai cấu hình thành phần, cho thấy hệ thống đang cộng gộp thay vì hợp nhất. Đây là hạn chế do chính thực nghiệm chỉ ra (mục 7.1) và là việc cần sửa trước tiên.
8. **Cách chấm điểm phạt oan trường hợp một phát hiện bao trùm nhiều ground truth liền nhau** (xem case `secret-aws`, mục 7.1), làm recall báo cáo thấp hơn thực tế.

### Hướng phát triển
- Hỗ trợ GitLab/Bitbucket; suggestion block (cho phép nhấn "Apply" ngay trên GitHub).
- Dashboard web theo dõi chất lượng code theo thời gian.
- Vòng phản hồi 👍/👎 tự động cập nhật bộ nhớ repo.
- Đa người thuê (multi-tenant) và tính phí — hướng sản phẩm micro-SaaS.

---

## 10. Dự kiến câu hỏi phản biện và hướng trả lời

**H: Thực nghiệm cho thấy `both` kém hơn `llm`. Vậy phần static analysis có còn ý nghĩa không?**
Đ: Đây là kết quả **ngược với giả thuyết ban đầu** và em báo cáo đúng như đo được. Trên dataset này, tập lỗi static bắt được là tập con của tập LLM bắt được, nên static không thêm phát hiện đúng nào mà chỉ thêm dương tính giả. Tuy nhiên có ba điểm cần nói rõ. (1) Kết luận gắn với **lỗi tiêm nhân tạo, diff nhỏ** — điều kiện rất thuận lợi cho LLM; trên diff lớn hàng nghìn dòng, LLM dễ bỏ sót hơn còn static thì không phụ thuộc độ dài. (2) Static **tất định và có thể kiểm toán**: cùng đầu vào luôn cho cùng đầu ra, điều mà LLM không đảm bảo — đây là yêu cầu bắt buộc ở nhiều quy trình CI. (3) Vấn đề thực sự nằm ở **bước hợp nhất kết quả**, không phải ở bản thân static: FP của `both` xấp xỉ tổng FP hai nguồn, chứng tỏ dedupe đang cộng gộp thô. Hướng sửa là lọc theo độ tin cậy và gộp phát hiện trùng vị trí, thay vì bỏ static.

**H: Tại sao không dùng thẳng ChatGPT/Copilot để review?**
Đ: Ba lý do. (1) *An toàn*: gửi toàn bộ mã nguồn cho dịch vụ ngoài là rủi ro với repo nội bộ; kiến trúc này cho phép trỏ về model chạy nội bộ mà không sửa logic. (2) *Độ tin cậy*: LLM đơn lẻ hallucinate; hệ thống có bốn lớp kiểm chứng. (3) *Tích hợp quy trình*: tự động chạy khi mở PR, comment đúng dòng, cập nhật trạng thái Checks để chặn merge — không cần thao tác thủ công.

**H: Chia ba vai có thực sự cần thiết không, hay chỉ là chia prompt cho vui?**
Đ: Hai lý do kỹ thuật. (1) *Giảm nhiễu*: mỗi vai có checklist và thang severity riêng, tránh tình trạng một prompt khổng lồ khiến LLM trả lời hời hợt ở mọi khía cạnh. (2) *Tiết kiệm chi phí*: router chỉ kích hoạt vai cần thiết — PR chỉ sửa Dockerfile thì không gọi vai QA. Việc chứng minh định lượng lợi ích của kiến trúc đa vai là một hướng thực nghiệm mở rộng.

**H: Làm sao biết LLM không bịa lỗi?**
Đ: Bốn lớp: ràng buộc số dòng trong prompt, bước self-verify, kiểm tra kỹ thuật `commentable_line()` (dòng phải nằm trong diff), và dedupe ưu tiên static tool. Ngoài ra, thực nghiệm đo precision chính là để định lượng tỷ lệ bịa này.

**H: Nếu ai đó mở PR chứa mã độc thì sao?**
Đ: Mã trong PR **không bao giờ được thực thi**. Chỉ các công cụ static đọc file, trong container không có mạng, bỏ toàn bộ capability, source mount chỉ đọc, giới hạn CPU/RAM/số tiến trình. Kể cả khi một công cụ static bị khai thác, nó không có đường ra mạng.

**H: Tại sao precision chỉ khoảng 39–56%?**
Đ: Cần phân biệt hai loại dương tính giả. Loại thứ nhất là **bịa thật sự** — đây là vấn đề. Loại thứ hai là **phát hiện đúng nhưng nằm ngoài ground truth**: ví dụ khi tiêm lỗi SQL injection, công cụ cũng báo thêm "thiếu xử lý ngoại lệ" — đúng về mặt kỹ thuật nhưng không nằm trong danh sách lỗi đã tiêm. Cách chấm hiện tại tính cả hai là sai, nên precision báo cáo là **cận dưới**. Bước tiếp theo là rà thủ công để tách hai loại này.

**H: Chi phí vận hành bao nhiêu?**
Đ: Static tools miễn phí. Phần LLM đo được khoảng 7.000 token/PR ở cấu hình chỉ LLM. Với model hạng nhẹ, chi phí ở mức vài cent mỗi PR. Đây là một chỉ số được đo trong thực nghiệm.

**H: Em đã tự kiểm chứng hệ thống chạy đúng chưa, hay chỉ viết xong rồi để đó?**
Đ: Quá trình chạy thử phát hiện **10 lỗi thật** (mục 5), trong đó có ba lỗi khiến chức năng **hoàn toàn không hoạt động mà vẫn không báo lỗi**: semgrep chưa từng chạy, bandit chưa từng được gọi, và bước self-verify luôn thất bại trong im lặng. Nếu chỉ đọc code mà không chạy thực tế thì không thể phát hiện được những lỗi này.

---

## 11. Thuật ngữ

| Thuật ngữ | Giải thích |
|---|---|
| **Hallucination** | LLM tạo ra thông tin nghe hợp lý nhưng không đúng thực tế — ở đây là bịa ra lỗi không tồn tại |
| **Static analysis** | Phân tích mã nguồn mà không chạy chương trình |
| **Precision / Recall** | Precision: trong những gì báo, bao nhiêu phần đúng. Recall: trong những lỗi có thật, bắt được bao nhiêu phần |
| **F1** | Trung bình điều hòa của precision và recall — chỉ cao khi cả hai đều cao |
| **Ground truth** | Đáp án đúng đã biết trước, dùng làm chuẩn để chấm điểm |
| **Sandbox** | Môi trường cô lập để chạy thứ không đáng tin cậy |
| **Unified diff** | Định dạng chuẩn biểu diễn thay đổi giữa hai phiên bản file |
| **Webhook** | Cơ chế để dịch vụ ngoài (GitHub) chủ động gọi vào hệ thống khi có sự kiện |
| **HMAC** | Mã xác thực thông điệp dùng khóa bí mật — chứng minh webhook đến từ GitHub |
| **Idempotency** | Tính chất gọi nhiều lần cho kết quả như gọi một lần |
| **Self-verify** | Bước cho LLM tự kiểm chứng lại kết quả của chính mình |
