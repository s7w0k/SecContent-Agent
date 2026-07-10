"""SSE 端点 Query token 认证测试。"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest
from starlette.requests import Request
from starlette.responses import Response

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

from auth.deps import get_current_user
from main import auth_middleware


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/chat/ask_stream",
        "/api/articles/abc/drafts/0/revise_stream",
    ],
)
async def test_sse_endpoint_accepts_query_token(path):
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"token=valid-sse-token",
            "headers": [],
            "client": ("test", 123),
            "server": ("test", 80),
        },
    )
    captured_user_id = None

    async def call_next(authenticated_request: Request):
        nonlocal captured_user_id
        captured_user_id = await get_current_user(authenticated_request)
        return Response(status_code=200)

    with patch("main.decode_access_token", return_value={"sub": "sse-user"}) as decode:
        response = await auth_middleware(request, call_next)

    assert response.status_code == 200
    assert captured_user_id == "sse-user"
    decode.assert_called_once_with("valid-sse-token")
