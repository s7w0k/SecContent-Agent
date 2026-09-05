from __future__ import annotations

import hashlib
import json

import pytest
from agent.business_tools import (
    BusinessToolAdapterKind,
    BusinessToolExecutor,
    FakeBusinessToolAdapter,
    RecordedBusinessToolAdapter,
    SandboxBusinessToolAdapter,
    ToolRequestContext,
    build_business_tool_registry,
    detect_breaking_changes,
)
from agent.business_tools.execution import BusinessToolExecutionError


def _recording_key(name: str, args: dict) -> str:
    raw = json.dumps({"name": name, "args": args}, sort_keys=True, default=str, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def _fixtures() -> dict[str, dict]:
    article = {"article_id": "article-123456"}
    artifact = {
        "artifact_id": "draft-123456",
        "version": 1,
        "content_hash": "sha256:abc",
        "status": "draft",
    }
    return {
        "list_articles": {},
        "get_article": {"article_id": article["article_id"]},
        "search_news": {"query": "AI security"},
        "crawl_news": {"idempotency_key": "crawl-key-123"},
        "classify_article": {"article": article},
        "match_products": {"article": article},
        "score_article": {"article": article, "product_ids": ["ai-bom"]},
        "generate_draft": {
            "article": article,
            "product_ids": ["ai-bom"],
            "idempotency_key": "draft-key-123",
        },
        "review_draft": {"artifact": artifact},
        "revise_draft": {
            "artifact": artifact,
            "instruction": "shorten the opening",
            "expected_version": 1,
            "idempotency_key": "revise-key-123",
        },
        "save_draft_version": {
            "artifact": artifact,
            "expected_version": 1,
            "idempotency_key": "save-key-123",
        },
        "export_draft": {
            "artifact": artifact,
            "idempotency_key": "export-key-123",
        },
    }


def _context(scopes: frozenset[str] | None = None) -> ToolRequestContext:
    registry = build_business_tool_registry()
    all_scopes = frozenset(
        scope for name in registry.names() for scope in registry.get(name).required_scopes
    )
    return ToolRequestContext(user_id="user-a", tenant_id="tenant-a", scopes=scopes or all_scopes)


def test_registry_has_complete_versioned_contracts():
    registry = build_business_tool_registry()
    assert len(registry.names()) == 13  # 含 collect_product_evidence（LLM-Wiki 证据工具）
    assert registry.manifest_version.startswith("2.0:sha256:")
    for name in registry.names():
        contract = registry.get(name)
        assert contract.args_schema.model_json_schema()
        assert contract.result_schema.model_json_schema()
        assert contract.required_scopes
        assert contract.timeout_seconds > 0
        assert contract.adapters == {"fake", "recorded", "sandbox", "production"}


@pytest.mark.asyncio
async def test_every_fake_and_sandbox_result_passes_the_same_result_schema():
    registry = build_business_tool_registry()
    executor = BusinessToolExecutor(
        registry,
        {
            BusinessToolAdapterKind.FAKE: FakeBusinessToolAdapter(),
            BusinessToolAdapterKind.SANDBOX: SandboxBusinessToolAdapter(registry),
        },
    )
    for name, args in _fixtures().items():
        fake = await executor.invoke(name, args, context=_context(), adapter="fake")
        sandbox = await executor.invoke(name, args, context=_context(), adapter="sandbox")
        schema = registry.get(name).result_schema
        assert isinstance(fake, schema)
        assert isinstance(sandbox, schema)


@pytest.mark.asyncio
async def test_recorded_adapter_is_deterministic_and_schema_validated():
    registry = build_business_tool_registry()
    args = {"query": "recorded query"}
    normalized_args = (
        registry.get("search_news").args_schema.model_validate(args).model_dump(mode="python")
    )
    result = {"query": "recorded query", "items": [], "total": 0, "replay_ref": "r-1"}
    executor = BusinessToolExecutor(
        registry,
        {
            "recorded": RecordedBusinessToolAdapter(
                {_recording_key("search_news", normalized_args): result}
            )
        },
    )
    first = await executor.invoke("search_news", args, context=_context(), adapter="recorded")
    second = await executor.invoke("search_news", args, context=_context(), adapter="recorded")
    assert first == second
    assert first.replay_ref == "r-1"


@pytest.mark.asyncio
async def test_scope_and_path_traversal_are_rejected_before_adapter_execution():
    registry = build_business_tool_registry()
    executor = BusinessToolExecutor(registry, {"fake": FakeBusinessToolAdapter()})
    with pytest.raises(BusinessToolExecutionError, match="missing scopes"):
        await executor.invoke(
            "get_article",
            {"article_id": "article-123456"},
            context=_context(frozenset({"news:search"})),
            adapter="fake",
        )
    with pytest.raises(BusinessToolExecutionError) as exc:
        await executor.invoke(
            "export_draft",
            {
                "artifact": {
                    "artifact_id": "draft-123456",
                    "version": 1,
                    "content_hash": "sha256:abc",
                },
                "filename": "../secret",
                "idempotency_key": "export-key-123",
            },
            context=_context(),
            adapter="fake",
        )
    assert exc.value.code == "invalid_arguments"


@pytest.mark.asyncio
async def test_write_idempotency_returns_original_result():
    registry = build_business_tool_registry()
    calls = 0

    class CountingFake(FakeBusinessToolAdapter):
        async def invoke(self, contract, args, context):
            nonlocal calls
            calls += 1
            return await super().invoke(contract, args, context)

    executor = BusinessToolExecutor(registry, {"fake": CountingFake()})
    args = _fixtures()["generate_draft"]
    first = await executor.invoke("generate_draft", args, context=_context(), adapter="fake")
    second = await executor.invoke("generate_draft", args, context=_context(), adapter="fake")
    assert first == second
    assert calls == 1


def test_breaking_change_detector_catches_required_args_and_result_changes():
    registry = build_business_tool_registry()
    previous = registry.snapshot()
    current = json.loads(json.dumps(previous))
    current["tools"]["search_news"]["args_schema"]["required"].append("sources")
    current["tools"]["get_article"]["result_schema"]["title"] = "changed"
    changes = detect_breaking_changes(previous, current)
    assert {(item.tool_name, item.path) for item in changes} >= {
        ("search_news", "args.sources"),
        ("get_article", "result_schema"),
    }
