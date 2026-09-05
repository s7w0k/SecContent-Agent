"""评测可视化服务（类 Langfuse 只读视图）。

独立端口提供：
- GET /api/reports           评测报告列表（metadata 摘要）
- GET /api/reports/{name}    单份评测报告完整内容
- GET /api/products          产品目录（case_id 到产品中文名映射）
- /                         静态可视化页面（单文件 HTML）

数据源为项目根目录 reports/*.json，只读，不修改任何文件。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# 项目根目录 = 本文件 (services/backend/evalview/server.py) 向上三级
REPO_ROOT = Path(__file__).resolve().parents[3]
REPORTS_DIR = REPO_ROOT / "reports"
STATIC_DIR = Path(__file__).resolve().parent / "static"

# 报告清单：name -> (文件, 展示标题, 类型)
REPORT_MANIFEST: list[dict[str, str]] = [
    {
        "name": "baseline",
        "file": "knowledge-retrieval-baseline.json",
        "title": "产品路由基线评测",
        "type": "baseline",
    },
    {
        "name": "baseline-v2",
        "file": "knowledge-retrieval-baseline-v2.json",
        "title": "检索评测·真实用户输入",
        "type": "baseline",
    },
    {
        "name": "baseline-query",
        "file": "knowledge-retrieval-baseline-query.json",
        "title": "检索评测·线上用户Query",
        "type": "baseline",
    },
    {
        "name": "ranking",
        "file": "knowledge-retrieval-ranking.json",
        "title": "检索排序指标 (Recall@K/Precision@K/MRR/NDCG@K/HitRate)",
        "type": "ranking",
    },
    {
        "name": "ranking-query",
        "file": "knowledge-retrieval-ranking-query.json",
        "title": "检索排序指标·真实线上Query (Recall@K/Precision@K/MRR/NDCG@K/HitRate)",
        "type": "ranking",
    },
    {
        "name": "replay",
        "file": "knowledge-replay.json",
        "title": "知识检索离线回放",
        "type": "replay",
    },
    {
        "name": "eval-pr",
        "file": "eval-harness-pr-real_v1.json",
        "title": "PR 上下文评测",
        "type": "harness",
    },
    {
        "name": "eval-nightly",
        "file": "eval-harness-nightly-real_v1.json",
        "title": "夜间上下文评测",
        "type": "harness",
    },
]

app = FastAPI(title="知识检索评测可视化", version="1.0.0")


def _load_report(name: str) -> dict[str, Any]:
    for item in REPORT_MANIFEST:
        if item["name"] == name:
            path = REPORTS_DIR / item["file"]
            if not path.exists():
                raise HTTPException(status_code=404, detail=f"报告 {name} 不存在")
            with path.open(encoding="utf-8") as f:
                return json.load(f)
    raise HTTPException(status_code=404, detail=f"未知报告: {name}")


def _product_catalog() -> dict[str, str]:
    """case_id 前缀 -> 产品中文名 的映射（用于展示）。"""
    # 从 agent-security-briefs 目录推断产品映射
    mapping: dict[str, str] = {}
    briefs = REPO_ROOT / "agent-security-briefs"
    if briefs.exists():
        for cat in sorted(briefs.iterdir()):
            if cat.is_dir() and cat.name.startswith(("1-", "2-", "3-")):
                mapping.setdefault(cat.name[2:], cat.name[2:])
    return mapping


@app.get("/api/reports", response_model=list[dict[str, Any]])
async def list_reports() -> list[dict[str, Any]]:
    """报告列表 + 基础摘要（不代表全量数据，仅元数据）。"""
    summaries: list[dict[str, Any]] = []
    for item in REPORT_MANIFEST:
        path = REPORTS_DIR / item["file"]
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        summaries.append(
            {
                "name": item["name"],
                "title": item["title"],
                "type": item["type"],
                "file": item["file"],
                "generated_at": data.get("generated_at"),
                "total": data.get("total") or data.get("total_cases"),
                "passed": data.get("passed"),
                "modified": path.stat().st_mtime,
            }
        )
    return summaries


@app.get("/api/reports/{name}")
async def get_report(name: str) -> dict[str, Any]:
    return _load_report(name)


@app.get("/api/products")
async def get_products() -> dict[str, str]:
    return _product_catalog()


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="页面未构建")
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


# 静态资源（css/js 内联于 index.html，无需额外文件）
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(404)
async def not_found(_request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=404, content={"ok": False, "detail": str(exc.detail)})


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="info")


if __name__ == "__main__":
    main()
