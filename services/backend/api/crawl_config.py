"""Crawl account configuration API — list, add, delete WeChat accounts."""

import os
from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/crawl-config", tags=["CrawlConfig"])

COLLECTION = "crawl_accounts"
DEFAULTS = ["安恒信息", "奇安信集团", "绿盟科技"]


async def _ensure_defaults(db):
    """Ensure default accounts exist in DB."""
    if db is None:
        return
    existing = await db[COLLECTION].count_documents({})
    if existing == 0:
        for name in DEFAULTS:
            await db[COLLECTION].insert_one({"name": name.strip()})


@router.get("/accounts")
async def list_accounts(request: Request):
    """List all configured crawl accounts."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        # fallback to env
        accounts = os.getenv("JUST_ONE_ACCOUNTS", ",".join(DEFAULTS)).split(",")
        return {"accounts": [{"name": a.strip()} for a in accounts if a.strip()]}
    await _ensure_defaults(db)
    cursor = db[COLLECTION].find().sort("name", 1)
    accounts = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        accounts.append(doc)
    return {"accounts": accounts}


@router.post("/accounts")
async def add_account(request: Request, name: str):
    """Add a crawl account."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    existing = await db[COLLECTION].find_one({"name": name})
    if existing:
        raise HTTPException(status_code=409, detail="Account already exists")
    await db[COLLECTION].insert_one({"name": name})
    return {"ok": True, "name": name}


@router.delete("/accounts/{name}")
async def delete_account(name: str, request: Request):
    """Delete a crawl account."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    result = await db[COLLECTION].delete_one({"name": name})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"ok": True, "name": name}
