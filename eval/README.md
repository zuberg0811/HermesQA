# Eval HermesQA

Bộ đánh giá cho đồ án: đo precision/recall/F1, thời gian và token của 3 cấu hình
(chỉ static, chỉ LLM, kết hợp) trên 35 PR có lỗi biết trước.

## Thành phần
- `make_dataset.py` — sinh repo `eval/dataset-repo` (nhánh `main` sạch + 35 nhánh `case/<id>`,
  mỗi nhánh inject 1 lỗi) và `eval/ground_truth.json` (file, line, categories của từng lỗi).
  Phân bố: 12 security, 8 devops, 7 bug, 4 test, 4 style/performance.
- `run_eval.py` — checkout từng nhánh case, chạy `app.cli` theo từng cấu hình,
  lưu findings + thời gian + token vào `eval/out/`. Chạy lại sẽ tự bỏ qua case đã xong (resume).
- `score.py` — khớp findings với ground truth (cùng file, line ±3, đúng category),
  in bảng tổng hợp và ghi `eval/out/results.md`.

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
```

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
