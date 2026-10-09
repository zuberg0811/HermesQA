"""Gom dữ liệu cho dashboard từ hai nguồn đã có sẵn:
  1. Kết quả eval trên đĩa (eval/out*/ + ground_truth*.json) -> điểm theo cấu hình và THEO AGENT (có đáp án).
  2. Run log (app.runlog) -> chỉ số chất lượng của MỌI lần chạy, kể cả PR thật không có đáp án.

Không có cache: một lần chấm 35 case mất vài chục ms, và dashboard là công cụ nội bộ/demo.
"""
from __future__ import annotations

import importlib.util
import sys
from collections import defaultdict
from pathlib import Path

from app.config import settings
from app.runlog import ROLES, get_store

ROOT = Path(__file__).resolve().parents[2]

ROLE_INFO = {
    "SE": {"name": "Software Engineer", "focus": "Bug logic, bảo mật, hiệu năng, độ rõ ràng của code; chỉ file code không phải test/devops."},
    "QA": {"name": "QA Engineer", "focus": "Thiếu test cho hàm mới, assert yếu, test bị skip, nhánh lỗi chưa được kiểm; file code và file test."},
    "DevOps": {"name": "DevOps Engineer", "focus": "Dockerfile, CI, dependency, cấu hình: image chưa ghim, chạy root, secret trong cấu hình."},
}
TOOL_INFO = {
    "semgrep": "Luật mẫu đa ngôn ngữ (bảo mật, lỗi thường gặp)",
    "bandit": "Bảo mật Python", "gitleaks": "Khóa/secret lộ trong diff",
    "hadolint": "Dockerfile best practice", "ruff": "Lint Python",
}


def _score_module():
    """eval/score.py không phải package: nạp theo đường dẫn để dùng chung đúng hàm chấm của eval."""
    if "hermesqa_eval_score" in sys.modules:
        return sys.modules["hermesqa_eval_score"]
    path = ROOT / "eval" / "score.py"
    spec = importlib.util.spec_from_file_location("hermesqa_eval_score", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermesqa_eval_score"] = mod
    spec.loader.exec_module(mod)
    return mod


def eval_datasets() -> list[dict]:
    """Mọi thư mục eval/out* có kết quả chấm được, kèm ground truth tương ứng (multi -> ground_truth_multi)."""
    root = ROOT / settings.eval_root
    if not root.is_dir():
        return []
    score = _score_module()
    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not d.name.startswith("out"):
            continue
        truth = root / ("ground_truth_multi.json" if "multi" in d.name else "ground_truth.json")
        gt = score.load_json(str(truth), None)
        if gt is None:
            continue
        res = score.score_dir(gt, str(d), score.LINE_TOLERANCE)
        if not res["configs"]:
            continue
        res0 = score.score_dir(gt, str(d), 0)
        for cfg, s in res["configs"].items():
            s["f1_tol0"] = res0["configs"].get(cfg, {}).get("f1", 0.0)
        out.append({"name": d.name, "dir": str(d), "truth": truth.name, "cases": len(gt["cases"]),
                    "truths": sum(len(c["truth"]) for c in gt["cases"].values()), "results": res})
    return out


def agents_overview(limit_runs: int = 200) -> dict:
    """Tab Agent: (a) điểm theo agent trên từng bộ eval (có đáp án); (b) chỉ số theo vai gom từ run log."""
    datasets = eval_datasets()
    by_dataset = []
    for ds in datasets:
        rows = []
        for cfg, s in ds["results"]["configs"].items():
            for agent, v in s["by_agent"].items():
                rows.append({"config": cfg, "agent": agent, **v})
        by_dataset.append({"name": ds["name"], "rows": rows})

    runs = get_store().list_runs(limit_runs)
    agg: dict[str, dict] = {r: defaultdict(float) for r in ROLES}
    for run in runs:
        for role, rs in (run.get("role_stats") or {}).items():
            a = agg.setdefault(role, defaultdict(float))
            a["runs"] += 1
            for k in ("calls", "raw", "kept", "final", "removed", "tokens_in", "tokens_out", "failed", "verify_calls"):
                a[k] += rs.get(k, 0) or 0
    role_rows = []
    for role, a in agg.items():
        n = a.get("runs", 0) or 0
        role_rows.append({
            "role": role, "runs": int(n),
            "avg_raw": a["raw"] / n if n else 0, "avg_kept": a["kept"] / n if n else 0,
            "avg_final": a["final"] / n if n else 0,
            "removed_rate": a["removed"] / a["raw"] if a["raw"] else 0,
            "avg_tokens": (a["tokens_in"] + a["tokens_out"]) / n if n else 0,
            "failed": int(a["failed"]),
            "info": ROLE_INFO.get(role, {"name": role, "focus": ""}),
        })
    return {"by_dataset": by_dataset, "role_rows": role_rows, "n_runs": len(runs),
            "role_info": ROLE_INFO, "tool_info": TOOL_INFO}


def recent_runs(limit: int = 50) -> list[dict]:
    return get_store().list_runs(limit)


def run_detail(run_id: str) -> dict | None:
    rec = get_store().get(run_id)
    if not rec:
        return None
    by_role: dict[str, list] = defaultdict(list)
    for f in rec.get("findings", []):
        by_role[f.get("role") if f.get("source") == "llm" else f.get("source", "static")].append(f)
    rec["findings_by_agent"] = dict(by_role)
    return rec


def overview() -> dict:
    datasets = eval_datasets()
    headline = []
    for ds in datasets:
        for cfg in ("llm", "both"):
            s = ds["results"]["configs"].get(cfg)
            if s:
                headline.append({"dataset": ds["name"], "config": cfg,
                                 **{k: s[k] for k in ("tp", "fp", "fn", "precision", "recall", "f1", "f1_tol0")}})
    runs = recent_runs(10)
    return {"datasets": datasets, "headline": headline, "runs": runs, "store": type(get_store()).__name__,
            "model": settings.anthropic_model if settings.agent_backend == "anthropic" else settings.llm_model}
