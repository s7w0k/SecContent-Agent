"""
账号管理工具

提供 WeWe RSS 微信读书账号的检测与重新登录功能。
所有函数返回结构化 dict，便于 MCP / 程序化调用。
"""

import base64
import io
import time

from .trpc_client import TrpcError, mutation, query

STATUS_LABEL = {
    0: "失效",
    1: "正常",
    2: "禁用",
}


# ---------------------------------------------------------------------------
#  账号查询
# ---------------------------------------------------------------------------


def check_accounts() -> dict:
    """
    查询所有账号状态。

    Returns:
        {
            "accounts": [{id, name, status, status_label, is_blocked}],
            "total": int,
            "valid": int,
            "invalid": int,
            "disabled": int,
            "blocked_today": int,
            "has_usable": bool,
            "all_dead": bool,
        }
    """
    data = query("account.list", {"limit": 100})
    accounts = data.get("items", [])
    blocked_ids = set(data.get("blocks", []))

    result_accounts = []
    valid = invalid = disabled = blocked_today = 0

    for acc in accounts:
        acc_id = acc["id"]
        name = acc["name"]
        status = acc["status"]
        is_blocked = acc_id in blocked_ids

        result_accounts.append(
            {
                "id": acc_id,
                "name": name,
                "status": status,
                "status_label": STATUS_LABEL.get(status, "未知"),
                "is_blocked": is_blocked,
            }
        )

        if status == 1 and not is_blocked:
            valid += 1
        elif status == 0:
            invalid += 1
        elif status == 2:
            disabled += 1

        if is_blocked:
            blocked_today += 1

    total = len(accounts)
    has_usable = valid > 0
    all_dead = total > 0 and not has_usable

    return {
        "accounts": result_accounts,
        "total": total,
        "valid": valid,
        "invalid": invalid,
        "disabled": disabled,
        "blocked_today": blocked_today,
        "has_usable": has_usable,
        "all_dead": all_dead,
    }


def has_usable_account() -> bool:
    """检查是否有可用的账号（启用 + 未在小黑屋）"""
    info = check_accounts()
    return info["has_usable"]


# ---------------------------------------------------------------------------
#  登录流程（三步）
# ---------------------------------------------------------------------------


def create_login_qrcode() -> dict:
    """
    第一步：创建登录二维码。

    Returns:
        {"ok": true, "uuid": "...", "scan_url": "...", "qr_base64": "..."}
        {"ok": false, "error": "..."}
    """
    try:
        data = mutation("platform.createLoginUrl")
    except TrpcError as e:
        return {"ok": False, "error": str(e)}

    uuid = data["uuid"]
    scan_url = data["scanUrl"]

    # 同时生成 base64 PNG 二维码，MCP 智能体可直接展示
    qr_base64 = ""
    try:
        import qrcode as qrlib

        qr = qrlib.QRCode(border=2)
        qr.add_data(scan_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qr_base64 = base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        pass

    return {
        "ok": True,
        "uuid": uuid,
        "scan_url": scan_url,
        "qr_base64": qr_base64,
    }


def poll_login_result(uuid: str, timeout_seconds: int = 120) -> dict:
    """
    第二步：轮询扫码结果。

    Args:
        uuid: create_login_qrcode 返回的 uuid
        timeout_seconds: 超时秒数

    Returns:
        {"ok": true, "vid": "...", "token": "...", "name": "..."}
        {"ok": false, "error": "...", "stage": "timeout" | "login_failed"}
    """
    interval = 3
    start = time.time()

    while time.time() - start < timeout_seconds:
        try:
            result = query("platform.getLoginResult", {"id": uuid})
        except TrpcError:
            time.sleep(interval)
            continue

        if result.get("vid") and result.get("token"):
            return {
                "ok": True,
                "vid": str(result["vid"]),
                "token": result["token"],
                "name": result.get("username", ""),
            }

        if result.get("message"):
            return {
                "ok": False,
                "error": result["message"],
                "stage": "login_failed",
            }

        time.sleep(interval)

    return {
        "ok": False,
        "error": f"扫码超时（{timeout_seconds} 秒）",
        "stage": "timeout",
    }


def save_account(vid: str, token: str, name: str) -> dict:
    """
    第三步：保存账号到 WeWe RSS（upsert，自动启用）。

    Returns:
        {"ok": true, "id": "..."}
        {"ok": false, "error": "..."}
    """
    try:
        mutation(
            "account.add",
            {
                "id": vid,
                "token": token,
                "name": name,
                "status": 1,
            },
        )
        return {"ok": True, "id": vid}
    except TrpcError as e:
        return {"ok": False, "error": str(e)}


def delete_account(account_id: str) -> dict:
    """
    删除指定账号。

    Returns:
        {"ok": true}
        {"ok": false, "error": "..."}
    """
    try:
        mutation("account.delete", account_id)
        return {"ok": True}
    except TrpcError as e:
        return {"ok": False, "error": str(e)}
