"""Áp lại bước hậu xử lý HIỆN TẠI (lọc nhiễu static + dedupe) lên findings đã lưu.

Vì sao có script này: static tools là tất định, còn lời gọi LLM thì tốn quota và
không lặp lại được y hệt. Khi chỉ sửa bước hậu xử lý (postprocess.dedupe, bộ lọc
nhiễu trong static_tools) thì không cần chạy lại toàn bộ eval: lấy findings thô
đã lưu, chạy qua hậu xử lý mới, ghi ra thư mục khác rồi chấm bằng score.py.

Giới hạn (phải ghi rõ trong báo cáo): với cấu hình `both`, LLM đã nhìn thấy bản
tóm tắt static CŨ (chưa lọc nhiễu) trong prompt. Rescore không thay đổi phần
LLM đã sinh, chỉ gộp/lọc lại. Muốn số liệu "sạch" hoàn toàn phải chạy lại
run_eval.py.

Dùng:
  python eval/rescore.py --src eval/out --dst eval/out-v2
  python eval/score.py --out eval/out-v2
Thời gian/token: copy nguyên runs_<cfg>.json từ --src (hậu xử lý không đổi hai số đó).
"""
import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GITHUB_APP_ID", "1")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "x")

from app.review import postprocess  # noqa: E402
from app.review.schemas import Finding  # noqa: E402
from app.review.static_tools import is_noise  # noqa: E402


def reprocess(raw: list[dict]) -> list[dict]:
    findings = [Finding.model_validate(r) for r in raw]
    findings = [f for f in findings if not is_noise(f)]
    return [f.model_dump() for f in postprocess.dedupe(findings)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join("eval", "out"))
    ap.add_argument("--dst", required=True)
    a = ap.parse_args()

    os.makedirs(a.dst, exist_ok=True)
    n_cfg = 0
    for cfg in sorted(os.listdir(a.src)):
        src_dir = os.path.join(a.src, cfg)
        if not os.path.isdir(src_dir):
            continue
        dst_dir = os.path.join(a.dst, cfg)
        os.makedirs(dst_dir, exist_ok=True)
        before = after = 0
        for name in os.listdir(src_dir):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(src_dir, name), encoding="utf-8") as f:
                raw = json.load(f)
            out = reprocess(raw)
            before += len(raw); after += len(out)
            with open(os.path.join(dst_dir, name), "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
        runs = os.path.join(a.src, f"runs_{cfg}.json")
        if os.path.exists(runs):
            shutil.copy(runs, os.path.join(a.dst, f"runs_{cfg}.json"))
        print(f"{cfg}: {before} -> {after} findings")
        n_cfg += 1
    print(f"Xong {n_cfg} config -> {a.dst}. Chấm: python eval/score.py --out {a.dst}")


if __name__ == "__main__":
    main()
