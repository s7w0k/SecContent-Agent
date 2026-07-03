"""Operational logs API with date-based filtering."""

import contextlib
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request

router = APIRouter(prefix="/api/logs", tags=["Logs"])

LOG_COLLECTION = "pipeline_logs"


def _tz():
    return timezone(timedelta(hours=8))


async def _log_to_db(db, level: str, phase: str, message: str, detail: dict | None = None):
    """Write a log entry to MongoDB."""
    if db is None:
        return
    with contextlib.suppress(Exception):
        await db[LOG_COLLECTION].insert_one({
            "level": level,
            "phase": phase,
            "message": message,
            "detail": detail or {},
            "created_at": datetime.now(_tz()).strftime("%Y-%m-%d %H:%M:%S"),
            "date": datetime.now(_tz()).strftime("%Y-%m-%d"),
        })


def log_pipeline(db, level: str, phase: str, message: str, **detail):
    """Helper to log pipeline events (non-blocking)."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(_log_to_db(db, level, phase, message, detail))
    except Exception:
        pass


# ---- API Endpoints ----

@router.get("/dates")
async def list_dates(request: Request):
    """List all dates that have logs."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return {"dates": []}
    dates = await db[LOG_COLLECTION].distinct("date")
    return {"dates": sorted(dates, reverse=True)}


@router.get("/{date}")
async def get_logs_by_date(
    date: str,
    request: Request,
    phase: str = Query(default="", description="Filter by phase"),
    limit: int = Query(default=200, le=500),
):
    """Get logs for a specific date, optionally filtered by phase."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return {"date": date, "logs": [], "phases": []}

    query = {"date": date}
    if phase:
        query["phase"] = phase

    cursor = db[LOG_COLLECTION].find(query).sort("created_at", -1).limit(limit)
    logs = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        logs.append(doc)
    logs.reverse()

    phases = await db[LOG_COLLECTION].distinct("phase", {"date": date})

    return {"date": date, "logs": logs, "phases": sorted(phases)}
