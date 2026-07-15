"""C6 同机双 Compose Project 部署契约测试。"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = ROOT / "docker-compose.yml"
CRAWLER_COMPOSE = ROOT / "deploy" / "crawler" / "docker-compose.yml"
LOCAL_OVERLAY = ROOT / "deploy" / "local" / "docker-compose.core-local.yml"


def test_base_compose_allows_project_scoped_names_and_networks() -> None:
    raw = BASE_COMPOSE.read_text(encoding="utf-8")
    compose = yaml.safe_load(raw)

    assert "container_name:" not in raw
    assert "ipam" not in compose["networks"]["agent-net"]
    assert compose["services"]["backend-worker"]["environment"]["ARQ_JOB_TIMEOUT"] == (
        "${ARQ_JOB_TIMEOUT:-600}"
    )


def test_local_core_overlay_reuses_prebuilt_images() -> None:
    overlay = yaml.safe_load(LOCAL_OVERLAY.read_text(encoding="utf-8"))

    assert overlay["services"]["backend"]["image"].startswith("${CORE_BACKEND_IMAGE")
    assert overlay["services"]["backend-worker"]["image"].startswith("${CORE_BACKEND_IMAGE")
    assert overlay["services"]["mcp-wewe"]["image"].startswith("${CORE_WEWE_IMAGE")


def test_local_projects_have_separate_network_contracts() -> None:
    core = yaml.safe_load(BASE_COMPOSE.read_text(encoding="utf-8"))
    crawler = yaml.safe_load(CRAWLER_COMPOSE.read_text(encoding="utf-8"))

    assert set(core["networks"]) == {"agent-net"}
    assert set(crawler["networks"]) == {"crawler-net"}
    assert "mongodb" not in crawler["services"]
    assert set(crawler["services"]) == {"mcp-crawl"}


def test_local_environment_examples_use_matching_bridge_contract() -> None:
    crawler_env = (ROOT / "deploy" / "local" / ".env.crawler-local.example").read_text(
        encoding="utf-8"
    )
    core_env = (ROOT / "deploy" / "local" / ".env.core-local.example").read_text(encoding="utf-8")

    assert "MCP_CRAWL_BIND_HOST=0.0.0.0" in crawler_env
    assert "MCP_CRAWL_PORT=18101" in crawler_env
    assert "MCP_CRAWL_URL=http://host.docker.internal:18101" in core_env
    assert "BACKEND_PORT=18000" in core_env
    assert "MONGO_HOST_PORT=37017" in core_env
    assert "ARQ_JOB_TIMEOUT=3600" in core_env
