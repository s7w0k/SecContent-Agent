"""WeWe RSS account management API. All calls go directly to WeWe RSS tRPC."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import logging
from urllib.parse import quote
from urllib.request import urlopen

from fastapi import APIRouter, HTTPException
from fastapi.requests import Request as FastAPIRequest

logger = logging.getLogger("backend.api.accounts")
router = APIRouter(prefix="/api/accounts", tags=["Accounts"])

WEWE_BASE = "http://49.232.145.182:4001"
AUTH = "123567"


async def _trpc(procedure: str, params=None, method: str = "GET", timeout: int = 15) -> dict:
    """Direct tRPC call to WeWe RSS."""
    import urllib.request as _req
    input_str = json.dumps(params if params is not None else {}, ensure_ascii=False)
    url = f"{WEWE_BASE}/trpc/{procedure}?input={quote(input_str)}"
    body = input_str.encode("utf-8") if method == "POST" else None
    r = _req.Request(url, data=body, method=method)
    r.add_header("Authorization", AUTH)
    r.add_header("Content-Type", "application/json")
    logger.info(f"[accounts] tRPC {method} {procedure}")
    with _req.urlopen(r, timeout=timeout) as resp:  # nosec B310 - 内部 tRPC 服务固定 URL
        data = json.loads(resp.read())
    return data.get("result", {}).get("data", data)


# ---- Endpoints ----

@router.get("/status")
async def account_status(request: FastAPIRequest):
    try:
        data = await _trpc("account.list", {"limit": 100}, "GET")
        items = data.get("items", [])
        # status: 1=normal, 0=expired, 2=disabled
        labels = {0: "expired", 1: "active", 2: "disabled"}
        zhongwen = {0: "失效", 1: "正常", 2: "禁用"}
        accounts = []
        for a in items:
            s = a.get("status", 0)
            accounts.append({
                "id": a.get("id"), "name": a.get("name"),
                "status": labels.get(s, "unknown"),
                "status_label": zhongwen.get(s, "未知"),
                "status_code": s,
            })
        valid = sum(1 for a in items if a.get("status") == 1)
        return {"ok": True, "accounts": accounts, "total": len(items), "active_count": valid}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/qrcode")
async def create_qrcode(request: FastAPIRequest):
    try:
        data = await _trpc("platform.createLoginUrl", {}, "POST")
        uuid = data["uuid"]
        scan_url = data["scanUrl"]
        qr_base64 = ""
        try:
            import qrcode
            qr = qrcode.QRCode(border=2)
            qr.add_data(scan_url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            qr_base64 = base64.b64encode(buf.getvalue()).decode()
        except Exception:
            try:
                qr_api = f"https://api.qrserver.com/v1/create-qr-code/?size=200x200&data={quote(scan_url, safe='')}"
                qr_base64 = base64.b64encode(urlopen(qr_api, timeout=5).read()).decode()  # nosec B310 - 固定 https 二维码服务
            except Exception:
                pass
        return {"ok": True, "uuid": uuid, "scan_url": scan_url, "qr_base64": qr_base64}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/poll-login")
async def poll_login(uuid: str, timeout_seconds: int = 10):
    """Poll for QR scan via platform.getLoginResult (QUERY, not mutation)."""
    try:
        data = await _trpc("platform.getLoginResult", {"id": uuid}, "GET", timeout=timeout_seconds + 5)
        if data.get("vid"):
            return {"ok": True, "status": "confirmed",
                    "vid": str(data["vid"]), "token": data.get("token", str(data["vid"])),
                    "name": data.get("username", data.get("name", str(data["vid"])))}
        return {"ok": True, "status": "waiting"}
    except Exception:
        return {"ok": True, "status": "waiting"}


@router.post("/save")
async def save_account(vid: str, token: str, name: str):
    try:
        data = await _trpc("account.add", {"id": vid, "token": token, "name": name, "status": 1}, "POST")
        return {"ok": True, "id": data.get("id")}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.delete("/delete/{account_id}")
async def delete_account_route(account_id: str, request: FastAPIRequest):
    """Delete account from WeWe RSS + remove from crawl config."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="DB not available")
    # account.delete takes plain JSON string as input
    try:
        await _trpc("account.delete", account_id, "POST")
        logger.info(f"Account deleted from WeWe RSS: {account_id}")
    except Exception as e:
        logger.warning(f"WeWe RSS delete failed: {e}")
    # Remove from crawl config
    await db["crawl_accounts"].delete_many({"name": account_id})
    return {"ok": True}


@router.post("/toggle")
async def toggle_account(account_id: str, status: int):
    try:
        data = await _trpc("account.add", {"id": account_id, "name": account_id, "token": account_id, "status": status}, "POST")
        return {"ok": True, "id": data.get("id"), "status": data.get("status")}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/refresh")
async def refresh_articles(request: FastAPIRequest):
    try:
        await _trpc("feed.refreshArticles", {}, "POST", timeout=30)
        return {"ok": True, "message": "update triggered"}
    except Exception as e:
        body = ""
        if hasattr(e, "read"):
            with contextlib.suppress(Exception):
                body = e.read().decode()[:300]
        if "401" in str(e) or "401" in body:
            return {"ok": False, "message": "token expired, please re-login"}
        return {"ok": False, "message": str(e)[:100]}
