"""Route của dashboard. Server-render bằng Jinja2; HTMX chỉ dùng để tự làm mới danh sách lần chạy."""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.web import data

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["pct"] = lambda v: f"{(v or 0) * 100:.1f} %"
templates.env.filters["num"] = lambda v: f"{(v or 0):,.0f}".replace(",", " ")
templates.env.filters["f1"] = lambda v: f"{(v or 0):.1f}"


def _render(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"request": request, **ctx})


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request):
    return _render(request, "index.html", page="index", **data.overview())


@router.get("/agents", response_class=HTMLResponse)
def agents(request: Request):
    return _render(request, "agents.html", page="agents", **data.agents_overview())


@router.get("/runs", response_class=HTMLResponse)
def runs(request: Request, limit: int = 50):
    rows = data.recent_runs(limit)
    if request.headers.get("HX-Request"):
        return _render(request, "_runs_table.html", runs=rows)
    return _render(request, "runs.html", page="runs", runs=rows, limit=limit)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run(request: Request, run_id: str):
    rec = data.run_detail(run_id)
    if rec is None:
        raise HTTPException(404, "Không có lần chạy này")
    return _render(request, "run.html", page="runs", run=rec)


@router.get("/api/runs")
def api_runs(limit: int = 50):
    return data.recent_runs(limit)


@router.get("/api/runs/{run_id}")
def api_run(run_id: str):
    rec = data.run_detail(run_id)
    if rec is None:
        raise HTTPException(404, "Không có lần chạy này")
    return rec


@router.get("/api/eval")
def api_eval():
    return data.eval_datasets()
