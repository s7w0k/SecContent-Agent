"""C5 主体远程爬虫 Compose 解耦测试。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = ROOT / "docker-compose.yml"
REMOTE_COMPOSE = ROOT / "deploy" / "core" / "docker-compose.remote-crawl.yml"

CRAWL_SETTINGS = (
    "MCP_CRAWL_URL",
    "MCP_CRAWL_API_KEY",
    "MCP_CRAWL_CONNECT_TIMEOUT",
    "MCP_CRAWL_READ_TIMEOUT",
    "MCP_CRAWL_MAX_RETRIES",
    "MCP_CRAWL_MAX_RESPONSE_MB",
    "MCP_CRAWL_VERIFY_TLS",
)


def _service_block(compose: str, service: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(service)}:\s*$.*?(?=^  [a-zA-Z0-9_-]+:\s*$|\Z)",
        compose,
    )
    assert match is not None, f"service not found: {service}"
    return match.group(0)


def _depends_block(service_block: str) -> str:
    match = re.search(
        r"(?ms)^    depends_on:\s*$.*?(?=^    [a-zA-Z0-9_-]+:\s*$|\Z)",
        service_block,
    )
    return match.group(0) if match else ""


def test_base_compose_uses_identical_overridable_crawler_settings():
    compose = BASE_COMPOSE.read_text(encoding="utf-8")
    backend = _service_block(compose, "backend")
    worker = _service_block(compose, "backend-worker")

    for setting in CRAWL_SETTINGS:
        assert f"{setting}:" in backend
        assert f"{setting}:" in worker
    assert "${MCP_CRAWL_URL:-http://mcp-crawl:8101}" in backend
    assert "${MCP_CRAWL_URL:-http://mcp-crawl:8101}" in worker
    assert "mcp-crawl:" not in _depends_block(backend)
    assert "mcp-crawl:" not in _depends_block(worker)


def test_remote_override_disables_embedded_crawler_and_requires_credentials():
    override = REMOTE_COMPOSE.read_text(encoding="utf-8")
    crawler = _service_block(override, "mcp-crawl")
    backend = _service_block(override, "backend")
    worker = _service_block(override, "backend-worker")

    assert "profiles:" in crawler
    assert "embedded-crawl" in crawler
    for block in (backend, worker):
        assert "MCP_CRAWL_URL:?" in block
        assert "MCP_CRAWL_API_KEY:?" in block
        assert "host.docker.internal:host-gateway" in block
        for setting in CRAWL_SETTINGS:
            assert f"{setting}:" in block


@pytest.mark.skipif(
    shutil.which("docker") is None or not (ROOT / ".env").exists(),
    reason="Docker Compose and a local .env are required for semantic merge validation",
)
def test_remote_compose_resolves_to_five_core_services_with_matching_config():
    env = {
        **os.environ,
        "MCP_CRAWL_URL": "http://host.docker.internal:65535",
        "MCP_CRAWL_API_KEY": "test-c5-machine-token",
        "JWT_SECRET": "test-c5-jwt-secret-at-least-32-characters",
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(BASE_COMPOSE),
            "-f",
            str(REMOTE_COMPOSE),
            "config",
            "--no-path-resolution",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    config = json.loads(result.stdout)
    services = config["services"]

    assert set(services) == {
        "mongodb",
        "redis",
        "mcp-wewe",
        "searxng",
        "backend",
        "backend-worker",
    }
    for service_name in ("backend", "backend-worker"):
        service = services[service_name]
        assert "mcp-crawl" not in service.get("depends_on", {})
        assert "host.docker.internal=host-gateway" in service["extra_hosts"]
    backend_env = services["backend"]["environment"]
    worker_env = services["backend-worker"]["environment"]
    for setting in CRAWL_SETTINGS:
        assert backend_env[setting] == worker_env[setting]
