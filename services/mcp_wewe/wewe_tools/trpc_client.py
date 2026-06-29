"""
tRPC HTTP 客户端 — 封装 WeWe RSS 的 tRPC API 调用。

tRPC v10 协议：
  - GET  查询：参数放在 query string  ?input={urlencoded_json}
  - POST 变更：参数放在请求体 JSON 中
  - 需要 Authorization header
"""

import json
import urllib.request
import urllib.error

from .config import get_wewe_url, get_auth_code


class TrpcError(RuntimeError):
    """tRPC 调用错误"""
    pass


def call(method: str, procedure: str, params=None) -> dict:
    """
    调用 tRPC 接口。

    Args:
        method:  HTTP 方法 (GET / POST)
        procedure:  tRPC 过程名，如 "account.list"
        params:  输入参数 (dict / str / None)

    Returns:
        API 返回的 data 字段

    Raises:
        TrpcError: 调用失败
    """
    base_url = get_wewe_url().rstrip("/")
    auth_code = get_auth_code()

    input_str = json.dumps(params if params is not None else {}, ensure_ascii=False)
    input_encoded = urllib.parse.quote(input_str)

    url = f"{base_url}/trpc/{procedure}?input={input_encoded}"

    body_bytes = None
    if method == "POST":
        body_bytes = input_str.encode("utf-8")

    req = urllib.request.Request(url, data=body_bytes, method=method)
    req.add_header("Authorization", auth_code)
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = None
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            pass
        err = body.get("error", {}) if body else {}
        raise TrpcError(
            f"[{procedure}] {err.get('message', e.reason)} (HTTP {e.code})"
        )
    except urllib.error.URLError as e:
        raise TrpcError(f"[{procedure}] 连接失败: {e.reason}")

    result = body.get("result", {})
    if isinstance(result, dict):
        return result.get("data", result)
    return result


def query(procedure: str, params=None) -> dict:
    """GET 查询"""
    return call("GET", procedure, params)


def mutation(procedure: str, params=None) -> dict:
    """POST 变更"""
    return call("POST", procedure, params)
