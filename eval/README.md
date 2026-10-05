# Eval HermesQA

Bộ đánh giá cho đồ án: đo precision/recall/F1, thời gian và token của các cấu hình
(chỉ static, chỉ LLM, kết hợp, LLM tắt self-verify) trên 35 PR Python có lỗi biết trước,
cộng thêm bộ đa ngôn ngữ 18 PR (JS/TS/Java/Go).

## Thành phần
- `make_dataset.py` — sinh repo `eval/dataset-repo` (nhánh `main` sạch + 35 nhánh `case/<id>`,
  mỗi nhánh inject 1 lỗi) và `eval/ground_truth.json` (file, line, categories của từng lỗi).
  Phân bố: 12 security, 8 devops, 7 bug, 4 test, 4 style/performance.
- `run_eval.py` — checkout từng nhánh case, chạy `app.cli` theo từng cấu hình,
  lưu findings + thời gian + token vào `eval/out/`. Chạy lại sẽ tự bỏ qua case đã xong (resume).
- `make_dataset_multi.py` / `ground_truth_multi.json` — bộ đa ngôn ngữ (18 case) trong
  `eval/dataset-repo-multi`. Chạy eval/score với `--truth eval/ground_truth_multi.json --out eval/out-multi`.
- `score.py` — khớp findings với ground truth (cùng file, line ±3, đúng category),
  in bảng tổng hợp và ghi `<out>/results.md`. Tự phát hiện mọi config có trong thư mục out.
- `rescore.py` — áp lại bước hậu xử lý HIỆN TẠI (lọc nhiễu static + `postprocess.dedupe`)
  lên findings đã lưu, ghi ra thư mục khác để chấm. Dùng khi chỉ sửa hậu xử lý, không
  muốn tốn quota LLM chạy lại. Giới hạn: với `both`, prompt LLM đã nhìn thấy tóm tắt static
  cũ; rescore không đổi phần LLM đã sinh.

## Chạy
```bash
# 1. Sinh dataset (chỉ cần 1 lần; chạy lại sẽ tạo mới từ đầu)
python eval/make_dataset.py

# 2. Chạy các cấu hình
python eval/run_eval.py --configs static       # miễn phí, cần Docker + image hermesqa-runner
python eval/run_eval.py --configs llm,both     # cần LLM_API_KEY thật trong .env
#   thử trước với ít case:  --limit 5   hoặc  --cases sqli-concat,secret-aws

# 3. Chấm điểm
python eval/score.py                           # -> eval/out/results.md

# 4. (tuỳ chọn) Ablation self-verify và chấm lại sau khi sửa hậu xử lý
python eval/run_eval.py --configs llm_noverify
python eval/rescore.py --src eval/out --dst eval/out-v2
python eval/score.py --out eval/out-v2
```

## Các thư mục kết quả
| Thư mục | Nội dung |
|---|---|
| `eval/out` | Bộ Python, 4 config (`static`, `llm`, `both`, `llm_noverify`), output thô của pipeline lúc chạy |
| `eval/out-v2` | `eval/out` sau khi rescore bằng hậu xử lý hiện tại (dedupe nhận biết nguồn + lọc B101) |
| `eval/out-multi` / `eval/out-multi-v2` | Bộ đa ngôn ngữ, thô / rescore |
| `eval/out-noise` | Lần chạy `llm` thứ hai y hệt cấu hình, để đo dao động giữa hai lần chạy LLM |
| `eval/out-both-v2` / `eval/out-both-v2-fixed` | Chạy lại thật `both` ngày 05/10 với dedupe mới; bản `-fixed` là rescore sau khi nối bộ lọc B101 vào `collect()` (bug D3) |

## Ghi chú phương pháp
- `run_eval.py` đặt `MIN_SEVERITY_TO_COMMENT=low` và `MIN_CONFIDENCE=0.0` cho tiến trình con
  vì eval đo *khả năng phát hiện*; policy lọc comment của sản phẩm được đánh giá riêng.
- Một finding là true positive nếu khớp một ground truth chưa được khớp: cùng file,
  lệch dòng ≤ 3, category nằm trong danh sách chấp nhận (VD lỗi Dockerfile chấp nhận
  cả `devops` lẫn `security`). Mỗi ground truth chỉ được khớp 1 lần → báo trùng bị tính là FP.
- FP ở đây gồm cả những phát hiện "đúng nhưng ngoài ground truth" (VD LLM chê thiếu docstring),
  nên precision là cận dưới. Có thể rà tay `eval/out/<config>/<case>.json` để phân loại lại.
- Token chỉ đếm được với backend `openai_compat` (đọc từ log `tokens in/out` của agent).

## Lưu ý khi chạy với LLM free tier
- Gemini free tier giới hạn **theo từng model**: `gemini-3.6-flash` chỉ 20 request/ngày
  (không đủ vì mỗi case tốn ~4–6 request: mỗi vai 1 lượt review + 1 lượt self-verify).
  Các model `*-flash-lite` có hạn mức cao hơn nhiều — cấu hình đang dùng `gemini-3.1-flash-lite`.
- Khi gặp 429/503, client tự retry có backoff (`max_retries=5`). Nếu một vai vẫn lỗi,
  `app.cli` ghi kết quả phần còn lại rồi thoát mã 2; `run_eval.py` sẽ chạy lại case đó ở lần sau.

## Hiệu năng
`run_static.sh` chỉ quét các file trong diff và chỉ nạp rule semgrep theo ngôn ngữ xuất hiện
trong diff (thay vì cả 2150 rule). Thời gian một lần quét giảm từ ~350s xuống ~70–120s,
kết quả không đổi vì finding ngoài file thay đổi vốn đã bị lọc bỏ.
