# Eval cấu hình `llm` với Claude Code headless (claude-opus-5-5) — 14/10/2026

Chạy `python eval/run_eval.py --configs llm --profile claudecode --out eval/out-claudecode` qua
`claude-server/server.js` (endpoint OpenAI-compatible, tài khoản `claude login`, effort `medium`,
system prompt = preset `claude_code` + SKILL.md của vai). Chấm bằng `eval/score.py --out eval/out-claudecode`.
Cùng prompt, luật, fact-check và cách ghim với các lần chạy Gemini nên so trực tiếp được.

## Tổng hợp (35 case Python, cấu hình `llm`)

| Model | TP | FP | FN | Precision | Recall | F1 | s/PR (TB) | s/PR (trung vị) | tokens/PR |
|---|---|---|---|---|---|---|---|---|---|
| gemini-3.1-flash-lite (`eval/out-v3-rescored`) | 34 | 6 | 2 | 85,0% | 94,4% | 89,5% | 18,5 | – | 6 355 |
| claude-opus-5-5 qua Claude Code (`eval/out-claudecode`) | 34 | 20 | 2 | 63,0% | 94,4% | 75,6% | 67,2* | 35,1 | 23 217 |

\* Trung bình bị kéo lên bởi một case (`sqli-fstring`) treo 1 178 s do một request phía Claude không trả lời
trong 19 phút (không phải lỗi pipeline; đã thêm `REQUEST_TIMEOUT_MS` vào server sau sự cố). Bỏ case đó:
trung bình 34,5 s/PR trên 34 case. 118 request, 0 lỗi, 0 `api_retry`. Chi phí theo giá niêm yết API ≈ 4,49 USD
(thực tế chạy bằng quota tài khoản Claude Code).

Token/PR cao gấp ~3,7 lần Gemini vì preset system prompt của Claude Code (~5 000 token) được cộng vào mỗi request
(4 request/PR: 2 vai × (review + fact-check)); phần SKILL.md + diff chỉ ~2 500 token.

## Theo category (bắt được / tổng)

| Category | Gemini | Claude |
|---|---|---|
| bug | 7/7 | 7/7 |
| devops | 8/8 | 7/8 |
| performance | 1/1 | 1/1 |
| security | 12/13 | 12/13 |
| style | 3/3 | 3/3 |
| test | 3/4 | 4/4 |

Bỏ sót: Gemini `secret-aws`, `new-func-no-test`; Claude `secret-aws`, `docker-sudo`.
`docker-sudo`: Claude báo đúng dòng 4 ("`sudo` không có trong python:3.12-slim nên build fail") nhưng gắn
category `bug` thay vì `devops`/`security` nên bị chấm là miss + FP.

## Theo vai

| Vai | Gemini: finding / TP / FP / P | Claude: finding / TP / FP / P |
|---|---|---|
| SE | 22 / 22 / 0 / 100% | 21 / 16 / 5 / 76,2% |
| QA | 9 / 4 / 5 / 44,4% | 23 / 11 / 12 / 47,8% |
| DevOps | 9 / 8 / 1 / 88,9% | 10 / 7 / 3 / 70,0% |

## 20 FP của Claude là gì

Theo category: bug 9, test 8, devops 1, performance 1, style 1. Đọc tay từng cái:

- **8 "thiếu test cho hàm/endpoint mới"** (QA): đúng sự thật với mọi case (dataset chỉ thêm code, không thêm test)
  nhưng ground truth chỉ ghi nhận điều đó ở case `new-func-no-test`.
- **Các "bug" kèm theo**: xử lý lỗi HTTP trước khi parse JSON (`requests-verify-false`), luôn trả 200 khi lệnh
  thất bại (`cmd-os-system`), không escape CSV (`string-concat-loop`), import không dùng (`unused-import`,
  đúng nhưng gắn `style` ở file khác dòng khác) … phần lớn là nhận xét hợp lý ngoài ground truth.
- **Đáng ngờ thật sự**: `md5-password` "hồi quy: hash sha256 cũ không còn khớp" (critical) là suy đoán về dữ liệu
  đã lưu; `flask-debug` "app không đăng ký route" cần đọc file khác.

Nếu dùng `eval/judge_fp.py` với model khác để phân loại "đúng nhưng ngoài ground truth" / "sai sự thật",
dự kiến precision điều chỉnh của Claude cao hơn đáng kể 63%. Chưa chạy vì `.env` chưa có profile Gemini.

## Ablation (chạy thêm cùng ngày, cùng 35 case, cấu hình `llm`)

| Cấu hình server | TP | FP | FN | Precision | Recall | F1 | s/PR (trung vị) | tokens/PR | Thư mục |
|---|---|---|---|---|---|---|---|---|---|
| preset + effort medium (gốc) | 34 | 20 | 2 | 63,0% | 94,4% | 75,6% | 35,1 | 23 217 | `eval/out-claudecode` |
| **custom** (chỉ SKILL.md, không preset) + medium | 35 | 17 | 1 | **67,3%** | **97,2%** | **79,5%** | 36,5 | **14 163** | `eval/out-claudecode-custom` |
| preset + **effort low** | 35 | 21 | 1 | 62,5% | 97,2% | 76,1% | 33,2 | 22 652 | `eval/out-claudecode-low` |
| gemini-3.1-flash-lite (tham chiếu) | 34 | 6 | 2 | 85,0% | 94,4% | 89,5% | 18,5 (TB) | 6 355 | `eval/out-v3-rescored` |

Cả ba run Claude: 117–118 request, 0 lỗi, 0 retry, 0 timeout (sự cố treo 19 phút chỉ xảy ra một lần ở run gốc).
Chi phí theo giá niêm yết: custom ≈ 3,95 USD, low ≈ 4,06 USD (chạy bằng quota Claude Code).

Theo vai (finding / TP / FP / precision):

| Vai | preset+medium | custom+medium | preset+low |
|---|---|---|---|
| SE | 21 / 16 / 5 / 76% | 20 / 17 / 3 / 85% | 26 / 18 / 8 / 69% |
| QA | 23 / 11 / 12 / 48% | 23 / 10 / 13 / 44% | 19 / 9 / 10 / 47% |
| DevOps | 10 / 7 / 3 / 70% | 9 / 8 / 1 / 89% | 11 / 8 / 3 / 73% |

Nhận xét:

- **Bỏ preset (`SYSTEM_MODE=custom`) là cấu hình tốt nhất**: ít token nhất (giảm 39%), precision và recall đều
  cao hơn, thời gian tương đương. Preset system prompt của Claude Code viết cho tác vụ coding agent có tool,
  không giúp gì cho việc trả JSON review và còn làm loãng SKILL.md. Khuyến nghị đặt `SYSTEM_MODE=custom`
  làm mặc định cho hermesQA.
- **Effort low** nhanh hơn ~10% và bắt thêm được `docker-sudo` (35/36) nhưng không giảm token đầu vào
  (preset vẫn chiếm phần lớn) và precision không đổi. Có thể kết hợp custom + low để vừa rẻ vừa nhanh (chưa đo).
- Dao động giữa hai lần chạy y hệt với Gemini từng đo được ~0,7 điểm F1; chênh lệch 3,9 điểm F1 giữa preset
  và custom lớn hơn mức đó, nhưng một lần chạy mỗi cấu hình chưa đủ để kết luận chắc chắn. Nên chạy lặp
  thêm 1 lần cho custom nếu đưa số này vào báo cáo.
- Vai QA vẫn là nguồn FP chính ở mọi cấu hình (chủ yếu "thiếu test cho hàm mới", đúng nhưng ngoài ground truth),
  giống hệt mô hình Gemini (QA 44%). Đây là đặc điểm của SKILL.md vai QA so với ground truth, không phải của model.

## Kết luận tạm

- Recall ngang Gemini (34/36), bắt đủ 4/4 lỗi `test` mà Gemini thiếu 1.
- Claude viết nhiều nhận xét hơn (54 so với 40), phần dư chủ yếu là nhận xét đúng nhưng ngoài ground truth,
  nên precision theo ground truth thấp hơn. Với sản phẩm thật, policy `MIN_SEVERITY_TO_COMMENT` /
  `MAX_COMMENTS_PER_PR` (eval đã tắt) sẽ cắt bớt phần này.
- Việc đáng thử tiếp: (1) chạy lại với `SYSTEM_MODE=custom` (không preset) để đo riêng ảnh hưởng của preset
  lên token và precision; (2) `CLAUDE_EFFORT=low` để so thời gian; (3) chạy `judge_fp.py` phân loại FP.

Thư mục kết quả: `eval/out-claudecode/` (gitignore theo `eval/out-*/`), nhật ký: `runs_llm.json`.
