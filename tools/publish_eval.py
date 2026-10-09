"""Đẩy bảng điểm eval (precision/recall/F1 theo cấu hình, theo loại lỗi, theo agent) lên Firestore
collection `hermesqa_eval` để website demo hiển thị tab "Benchmark" và "Chấm điểm agent".

Kết quả eval nằm trong thư mục eval/out*/ (findings từng case + runs_<config>.json); file gốc không
đẩy lên — chỉ đẩy bản tổng hợp do eval/score.py:score_dir() tính, mỗi thư mục một document (id = tên
thư mục, ví dụ "out-v3"). Danh sách lỗi bỏ sót được cắt còn MISSED_LIMIT dòng mỗi cấu hình.

Dùng:
  python tools/publish_eval.py                         # mọi thư mục eval/out* có kết quả
  python tools/publish_eval.py --only out-v3,out-multi-v3 --note "v3 chốt 07/10, gemini-3.1-flash-lite"
  python tools/publish_eval.py --local                 # không đẩy, chỉ ghi data/eval_published/<ten>.json để xem
Credentials: FIREBASE_CREDENTIALS trong .env (file service-account JSON) hoặc --credentials.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MISSED_LIMIT = 40


def _load_score_module():
    spec = importlib.util.spec_from_file_location("hqa_eval_score", os.path.join(ROOT, "eval", "score.py"))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def find_eval_dirs(eval_root: str) -> list[str]:
    """Thư mục con eval/out* có ít nhất một runs_<config>.json."""
    out = []
    for name in sorted(os.listdir(eval_root)):
        p = os.path.join(eval_root, name)
        if name.startswith("out") and os.path.isdir(p) and any(n.startswith("runs_") and n.endswith(".json") for n in os.listdir(p)):
            out.append(name)
    return out


def truth_for(name: str, eval_root: str) -> str:
    return os.path.join(eval_root, "ground_truth_multi.json" if "multi" in name else "ground_truth.json")


def build_doc(score, eval_root: str, name: str, tolerance: int, note: str) -> dict | None:
    truth_path = truth_for(name, eval_root)
    gt = score.load_json(truth_path, None)
    if gt is None:
        print(f"  bỏ qua {name}: không có {truth_path}")
        return None
    res = score.score_dir(gt, os.path.join(eval_root, name), tolerance)
    if not res["configs"]:
        return None
    for cfg in res["configs"].values():
        cfg["missed_total"] = len(cfg.get("missed", []))
        cfg["missed"] = cfg.get("missed", [])[:MISSED_LIMIT]
    return {"name": name, "truth": os.path.basename(truth_path), "tolerance": tolerance, "note": note,
            "cases_total": len(gt.get("cases", {})), "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "configs": res["configs"]}


def _credentials(arg: str | None) -> str:
    if arg:
        return arg
    if os.environ.get("FIREBASE_CREDENTIALS"):
        return os.environ["FIREBASE_CREDENTIALS"]
    env = os.path.join(ROOT, ".env")       # không import app.config để script chạy được cả khi thiếu GITHUB_APP_ID
    if os.path.exists(env):
        with open(env, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("FIREBASE_CREDENTIALS="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eval-root", default=os.path.join(ROOT, "eval"))
    ap.add_argument("--only", help="chỉ các thư mục này, phân tách bằng dấu phẩy")
    ap.add_argument("--tolerance", type=int, default=3)
    ap.add_argument("--note", default="", help="ghi chú hiển thị kèm (model, ngày chốt...)")
    ap.add_argument("--collection", default="hermesqa_eval")
    ap.add_argument("--credentials", help="file service-account JSON (mặc định FIREBASE_CREDENTIALS)")
    ap.add_argument("--local", action="store_true", help="chỉ ghi data/eval_published/, không đẩy Firestore")
    a = ap.parse_args()

    score = _load_score_module()
    names = find_eval_dirs(a.eval_root)
    if a.only:
        want = {n.strip() for n in a.only.split(",") if n.strip()}
        missing = want - set(names)
        if missing:
            print(f"không thấy kết quả cho: {', '.join(sorted(missing))}")
        names = [n for n in names if n in want]
    if not names:
        print("không có thư mục eval nào để đẩy")
        return 1

    docs = [d for d in (build_doc(score, a.eval_root, n, a.tolerance, a.note) for n in names) if d]
    out_dir = os.path.join(ROOT, "data", "eval_published")
    os.makedirs(out_dir, exist_ok=True)
    for d in docs:
        with open(os.path.join(out_dir, f"{d['name']}.json"), "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)
        line = " · ".join(f"{k}: F1 {v['f1']:.1%} ({v['cases']} case)" for k, v in d["configs"].items())
        print(f"{d['name']}: {line}")
    print(f"đã ghi {len(docs)} bản tổng hợp vào {out_dir}")
    if a.local:
        return 0

    cred = _credentials(a.credentials)
    if not cred:
        print("Thiếu FIREBASE_CREDENTIALS (file service-account JSON) — dùng --local nếu chỉ muốn xem.")
        return 2
    import firebase_admin
    from firebase_admin import credentials, firestore
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(cred))
    col = firestore.client().collection(a.collection)
    for d in docs:
        col.document(d["name"]).set(d)
        print(f"  -> Firestore {a.collection}/{d['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
