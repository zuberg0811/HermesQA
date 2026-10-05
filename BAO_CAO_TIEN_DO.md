# Báo cáo tiến độ đồ án: HermesQA — Bot AI tự động review Pull Request

**Cập nhật:** 05/10/2026 · **Trạng thái:** Hệ thống chạy đầu-cuối; thực nghiệm đã chạy đủ 2 bộ dữ liệu (Python 35 case, đa ngôn ngữ 18 case) + ablation self-verify; bước hợp nhất static/LLM đã sửa theo kết quả đo

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
4. **Gộp kết quả nhận biết nguồn (dedupe)**: hai finding cách nhau ≤ 2 dòng trong cùng file được coi là cùng vị trí. Giữa static tool và LLM, cùng vị trí là đủ để gộp (hai bên dùng hai taxonomy khác nhau cho cùng một lỗi: ruff gọi `== None` là *bug*, LLM gọi là *maintainability*), **với điều kiện** finding LLM không nghiêm trọng hơn finding static; nếu LLM cho rằng chỗ đó có lỗi nặng hơn (semgrep *medium* cạnh nil-deref *critical*) thì đó là hai lỗi khác nhau, giữ cả hai. Giữa hai vai LLM với nhau vẫn đòi cùng category, vì SE báo *bug* và QA báo *thiếu test* cho cùng hàm là hai nhận xét khác nhau. Finding chính là static tool (mô tả tất định); finding bị gộp được **đính kèm vào comment** chứ không vứt bỏ, và severity của cụm lấy mức cao nhất. Quy tắc này là kết quả của thực nghiệm (mục 7.4): bản đầu đòi cùng category giữa mọi nguồn nên gần như không gộp được gì.

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
| B6 | Ruff chưa từng chạy được trong sandbox | Ruff ghi cache vào `/src` đang mount read-only rồi thoát lỗi; lỗi bị nuốt | Tắt cache; thêm `failed_tools.txt` + `tool_status()` để report phân biệt "không có lỗi" với "công cụ chết"; thêm integration test chạy container thật |
| B7 | `cli.py` gọi `log.error` khi một static tool thất bại nhưng chưa khai báo `log` | Đúng lúc cần báo lỗi thì chính dòng báo lỗi crash bằng `NameError` — cùng lớp bug "công cụ im lặng" | Khai báo logger; phát hiện khi rà code để sửa bước dedupe |

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

### Nhóm D — Sửa theo kết quả thực nghiệm (05/10)

| # | Vấn đề đo được | Nguyên nhân | Cách sửa |
|---|---|---|---|
| D1 | FP của cấu hình `both` (61) **lớn hơn** tổng FP hai nguồn (26 + 27): bước gộp không gộp được gì | Dedupe đòi *cùng category* giữa mọi nguồn, trong khi static và LLM đặt tên loại lỗi khác nhau; LLM bị dặn "không lặp lại static" nên báo thêm nhận xét phụ ngay cạnh đó (19/35 FP LLM nằm trong ±2 dòng của một finding static) | Quy tắc gộp nhận biết nguồn (mục 4, điểm 4). Thử bản "gộp theo vị trí bất kể nguồn" trước: mất 2 TP vì gộp nhầm hai vai LLM; thử bản "finding nghiêm trọng hơn làm chính": LLM tự chấm *high* cho "thiếu unit test" đè lên finding static đúng, mất 3 TP. Bản cuối không đổi nhãn, không giấu finding nặng hơn |
| D2 | 11/26 FP của `static` là bandit **B101 `assert_used`** trong `tests/` | `assert` là cách viết chuẩn của pytest; tài liệu bandit khuyến nghị bỏ B101 cho test code | Bộ lọc nhiễu `is_noise()` trong `static_tools.py`, có bảng luật tường minh. Nguyên tắc: chỉ thêm luật là best practice được tài liệu công cụ ghi nhận, không thêm luật vì nó làm đẹp điểm eval |

Hai thay đổi này chỉ chạm bước hậu xử lý nên được kiểm chứng bằng `eval/rescore.py`: lấy findings thô đã lưu, chạy qua hậu xử lý mới, chấm lại (static tools tất định; phần LLM không sinh lại). Có **5 unit test** mới cho các quy tắc trên.

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

**Bộ thứ hai — đa ngôn ngữ (18 case):** để kiểm tra kết quả có đứng vững khi rời Python/Dockerfile không, `make_dataset_multi.py` sinh một repo riêng gồm frontend JS/JSX/TS (XSS qua innerHTML và dangerouslySetInnerHTML, `eval`, secret, so sánh lỏng `==`, thiếu `key` trong React, thiếu `await`, assertion yếu, skip test), Go (SQL injection, bỏ qua `err`, nil dereference, thiếu test) và Java (SQL injection, MD5, resource leak). Phân bố: 7 security, 7 bug, 3 test, 1 performance.

### 6.2 Các cấu hình so sánh
| Cấu hình | Mô tả | Mục đích |
|---|---|---|
| `static` | Chỉ 5 công cụ static | Đường cơ sở (baseline) |
| `llm` | Chỉ LLM 3 vai | Đo năng lực riêng của LLM |
| `both` | Kết hợp (mặc định của sản phẩm) | Kiểm chứng giả thuyết "kết hợp tốt hơn từng phần" |
| `llm_noverify` | Chỉ LLM, **tắt** self-verify | Ablation: đo self-verify có thật sự lọc bớt finding sai không |

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

Thực nghiệm đã chạy **đủ 35 case × 4 cấu hình** trên bộ Python và **18 case × 3 cấu hình** trên bộ đa ngôn ngữ, không lượt nào thất bại. Ngoài ra chạy lại `llm` một lần nữa y hệt cấu hình để đo dao động.

### 7.1 Bộ Python — số liệu thô lúc chạy (sau khi sửa ruff, trước khi sửa bước gộp)

| Cấu hình | TP | FP | FN | Precision | Recall | F1 | Thời gian/PR | Token/PR |
|---|---|---|---|---|---|---|---|---|
| `static` | 23 | 26 | 13 | 46,9 % | 63,9 % | 54,1 % | 101,4 s | 0 |
| `llm` | 34 | 27 | 2 | 55,7 % | 94,4 % | **70,1 %** | **13,3 s** | 7.229 |
| `both` | 35 | 61 | 1 | 36,5 % | **97,2 %** | 53,0 % | 119,2 s | 7.552 |
| `llm_noverify` | 34 | 28 | 2 | 54,8 % | 94,4 % | 69,4 % | 7,0 s | 3.324 |
| `llm` chạy lần 2 | 33 | 26 | 3 | 55,9 % | 91,7 % | 69,5 % | 19,9 s | 7.163 |

Tổng ground truth: 36 lỗi. Chi tiết từng case: `eval/out/results.md`.

### 7.2 Bộ Python — sau khi sửa bước gộp và lọc nhiễu (D1, D2), chấm lại bằng `rescore.py`

| Cấu hình | TP | FP | FN | Precision | Recall | F1 | So với 7.1 |
|---|---|---|---|---|---|---|---|
| `static` | 23 | 15 | 13 | 60,5 % | 63,9 % | 62,2 % | F1 +8,1 điểm (bỏ 11 FP B101) |
| `llm` | 34 | 27 | 2 | 55,7 % | 94,4 % | 70,1 % | không đổi |
| `both` | 35 | 34 | 1 | 50,7 % | **97,2 %** | 66,7 % | F1 +13,7 điểm, FP 61 → 34, **không mất TP nào** |

**Khả năng phát hiện theo nhóm lỗi (bắt được / tổng), sau 7.2:**

| Nhóm | `static` | `llm` | `both` |
|---|---|---|---|
| security | 12/13 | 12/13 | 12/13 |
| devops | 6/8 | 8/8 | 8/8 |
| bug | 4/7 | 6/7 | 7/7 |
| test | 0/4 | 4/4 | 4/4 |
| style | 1/3 | 3/3 | 3/3 |
| performance | 0/1 | 1/1 | 1/1 |

### 7.3 Bộ đa ngôn ngữ (18 case, JS/TS/Go/Java)

| Cấu hình | TP | FP | FN | Precision | Recall | F1 | Thời gian/PR | Token/PR |
|---|---|---|---|---|---|---|---|---|
| `static` | 6 | 1 | 12 | 85,7 % | 33,3 % | 48,0 % | 89,9 s | 0 |
| `llm` | 18 | 13 | 0 | 58,1 % | 100 % | **73,5 %** | 33,6 s | 8.336 |
| `both` (thô) | 18 | 16 | 0 | 52,9 % | 100 % | 69,2 % | 96,2 s | 8.570 |
| `both` (sau D1) | 18 | 14 | 0 | 56,2 % | 100 % | 72,0 % | – | – |

Static chỉ bắt được 6/18 (toàn bộ là security: SQLi, eval, secret, MD5) vì bộ rule semgrep nạp sẵn cho JS/Go/Java thưa hơn Python, và không có công cụ tương đương bandit/ruff cho các ngôn ngữ này. LLM bắt 18/18.

### 7.4 Nhận định

**1. Giả thuyết "kết hợp tốt hơn từng phần" chỉ đúng một nửa.** `both` có recall cao nhất trên cả hai bộ (97,2 % và 100 %), nhưng F1 vẫn thấp hơn `llm` thuần 1,5–3,4 điểm vì precision thấp hơn. Trước khi sửa D1/D2, khoảng cách là 17 điểm và FP của `both` **lớn hơn tổng FP hai nguồn** — tức bước gộp không hoạt động. Sau khi sửa, FP của `both` (34) đã **nhỏ hơn tổng** (15 + 27 = 42): bước gộp có tác dụng thật, không phải cộng gộp thô nữa.

**2. Static không còn là tập con của LLM.** Nhận định trước đây (static không đóng góp phát hiện nào LLM bỏ sót) dựa trên số liệu khi ruff chưa chạy được. Sau khi sửa ruff, static bắt được `eq-none` (so sánh `None` bằng `==`) mà LLM bỏ sót trong cả hai lần chạy, và bắt `undefined-name`, `bare-except`, `mutable-default-arg` tất định. Đó là lý do `both` đạt bug 7/7 trong khi `llm` chỉ 6/7.

**3. Phần FP còn lại của `both` chủ yếu là "đúng nhưng ngoài ground truth".** Rà 34 FP sau D1/D2: 5 lần `dockerfile-source-not-pinned` (semgrep, base image không ghim digest), 3 lần "thiếu HEALTHCHECK", 5 lần "thiếu đóng kết nối database" (base code của dataset thật sự không đóng kết nối), ~10 lần "thiếu unit test cho hàm X". Đây là nhận xét hợp lệ về mặt kỹ thuật nhưng không phải lỗi được tiêm, nên precision báo cáo là **cận dưới** (mục 6.3).

**4. Self-verify chưa chứng minh được lợi ích.** Bật/tắt self-verify chênh 0,7 điểm F1 (70,1 so với 69,4), **bằng** mức dao động giữa hai lần chạy `llm` y hệt (70,1 so với 69,5), trong khi tốn gấp đôi token (7.229 so với 3.324) và gấp đôi thời gian. Nguyên nhân có thể: self-verify dùng chính model và chính vai đã sinh finding, nên ít khi "đổi ý". Đây là kết quả âm đáng báo cáo, và là hướng mở: dùng model khác hoặc vai khác để kiểm chứng.

**5. Static chậm hơn LLM 3–10 lần** (101 s so với 13 s trên Python; 90 s so với 34 s trên đa ngôn ngữ) do khởi tạo container và chạy tuần tự 5 công cụ, không phải do khối lượng phân tích.

**6. Hai lỗi không cấu hình nào bắt được:** `secret-aws` dòng 7 (artifact của cách chấm: một finding bao trùm cặp access key/secret key ở dòng 6–7, mỗi ground truth chỉ khớp một finding) và, với `llm`, `eq-none`.

### 7.5 Hệ quả cho thiết kế sản phẩm

- Nếu tối ưu F1 và tốc độ: `llm` thuần vẫn nhỉnh hơn trên dataset này.
- Nếu ưu tiên **không bỏ sót** (recall) và cần kết quả **tất định, kiểm toán được** cho quy trình CI: `both` sau D1/D2 là lựa chọn hợp lý, với chi phí là ~7 FP thêm trên 35 PR và 100 s/PR cho sandbox. Phần FP này gần như toàn bộ là nhận xét hợp lệ ngoài ground truth.
- Self-verify nên là tuỳ chọn tắt mặc định cho đến khi có bằng chứng, vì tốn gấp đôi token mà không đo được lợi ích.

Kết luận gắn với **dataset lỗi tiêm nhân tạo, diff nhỏ** — điều kiện thuận lợi cho LLM. Trên diff lớn, LLM dễ bỏ sót còn static không phụ thuộc độ dài (mục 9).

### Kiểm thử
`pytest`: **8/8 unit test pass** (parse diff, router, 4 test cho quy tắc dedupe, lọc nhiễu bandit, hồi quy self-verify) · **8 integration test** chạy container thật, tự bỏ qua khi không có Docker.

---

## 8. Trạng thái các hạng mục

| Hạng mục | Trạng thái |
|---|---|
| Kiến trúc và toàn bộ mã nguồn | ✅ Hoàn thành (~1.700 dòng) |
| Sandbox static analysis (5 công cụ) | ✅ Đã chạy và kiểm chứng |
| Agent 3 vai + self-verify | ✅ Đã chạy đầu-cuối với Google Gemini |
| CLI chạy offline | ✅ Hoàn thành |
| Bộ thực nghiệm (dataset + chấm điểm) | ✅ Hoàn thành — bộ Python 35 case + bộ đa ngôn ngữ 18 case |
| Kiểm thử đơn vị / tích hợp | ✅ 8/8 unit pass; 8 integration test (cần Docker) |
| **Chạy thực nghiệm đầy đủ** | ✅ **Hoàn thành — 35×4 + 18×3 + 35 (lần 2) = 229 lượt, không lượt nào thất bại** |
| Sửa bước hợp nhất static/LLM theo kết quả đo | ✅ Xong 05/10 (D1, D2), kiểm chứng offline bằng `rescore.py` |
| Chạy lại `both` thật với hậu xử lý mới | ⏳ Chưa — cần Docker + quota LLM; số liệu 7.2 là rescore offline |
| Triển khai GitHub App thật | ⏳ Chưa — cần đăng ký GitHub App và tunnel công khai |
| Viết báo cáo/luận văn | 🔄 Đang viết — đã có đủ số liệu thực nghiệm |

---

## 9. Hạn chế và hướng phát triển

### Hạn chế hiện tại (nên chủ động nêu khi bảo vệ)
1. **Dataset là lỗi tiêm nhân tạo**, không phải PR thật từ dự án mở. Lỗi tiêm thường "sạch" và dễ nhận ra hơn lỗi thực tế, nên kết quả có thể lạc quan hơn thực tế.
2. **Cỡ mẫu 35 case** đủ để so sánh xu hướng nhưng chưa đủ để kết luận có ý nghĩa thống kê.
3. **Bộ đa ngôn ngữ còn nhỏ (18 case)** và static tools cho JS/Go/Java chỉ có semgrep, nên so sánh static/LLM ở đó nghiêng về LLM.
4. **Phụ thuộc vào một nhà cung cấp LLM**; kết quả có thể khác khi đổi model. (Kiến trúc đã tách qua `AgentBackend` protocol nên đổi model không phải sửa logic.)
5. **Bộ nhớ repo chưa có vòng phản hồi tự động**: hiện phải nhập tay, chưa học từ phản ứng 👍/👎 của lập trình viên.
6. **Chưa đo chi phí thực tế bằng tiền** trên quy mô lớn.
7. **Số liệu `both` sau khi sửa bước gộp (mục 7.2) là chấm lại offline**, không phải chạy lại: phần LLM vẫn là output sinh ra khi prompt còn chứa tóm tắt static cũ (có B101). Cần một lần chạy lại thật để xác nhận.
8. **Cách chấm điểm phạt oan trường hợp một phát hiện bao trùm nhiều ground truth liền nhau** (xem case `secret-aws`, mục 7.4), làm recall báo cáo thấp hơn thực tế.
9. **Self-verify dùng chính model đã sinh finding** nên chưa đo được lợi ích (mục 7.4, điểm 4).

### Hướng phát triển
- Hỗ trợ GitLab/Bitbucket; suggestion block (cho phép nhấn "Apply" ngay trên GitHub).
- Dashboard web theo dõi chất lượng code theo thời gian.
- Vòng phản hồi 👍/👎 tự động cập nhật bộ nhớ repo.
- Đa người thuê (multi-tenant) và tính phí — hướng sản phẩm micro-SaaS.

---

## 10. Dự kiến câu hỏi phản biện và hướng trả lời

**H: Thực nghiệm cho thấy `both` kém hơn `llm` về F1. Vậy phần static analysis có còn ý nghĩa không?**
Đ: Em báo cáo đúng như đo được: F1 của `both` thấp hơn `llm` 1,5–3,4 điểm. Nhưng có bốn điểm. (1) `both` có **recall cao nhất** trên cả hai bộ, và static bắt được lỗi LLM bỏ sót trong cả hai lần chạy (`eq-none`), nên static không phải tập con của LLM. (2) Khoảng cách ban đầu là 17 điểm, do bước gộp đòi cùng category nên không gộp được gì — thực nghiệm chỉ ra lỗi này và em đã sửa, sau đó FP của `both` nhỏ hơn tổng FP hai nguồn. (3) Phần FP còn lại chủ yếu là nhận xét hợp lệ ngoài ground truth (base image không ghim digest, thiếu đóng kết nối). (4) Static **tất định và kiểm toán được** — cùng đầu vào luôn cho cùng đầu ra, trong khi hai lần chạy `llm` y hệt đã lệch nhau 1 TP; đây là yêu cầu bắt buộc ở nhiều quy trình CI. Kết luận gắn với dataset lỗi tiêm, diff nhỏ — điều kiện thuận lợi cho LLM.

**H: Self-verify — đóng góp chính — có tác dụng không?**
Đ: Chưa chứng minh được. Em làm ablation: tắt self-verify thì F1 giảm 0,7 điểm, bằng đúng mức dao động giữa hai lần chạy LLM y hệt, trong khi tốn gấp đôi token. Em báo cáo đây là kết quả âm. Giả thuyết: kiểm chứng bằng chính model và chính vai đã sinh finding thì hiếm khi đổi ý; hướng sửa là dùng model hoặc vai khác làm người kiểm chứng.

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
