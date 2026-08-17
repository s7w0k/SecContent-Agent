from __future__ import annotations

import json
from pathlib import Path

from api.autonomous import router as autonomous_router
from api.chat import router as chat_router
from api.pipeline import router as pipeline_router

from scripts.lint_adrs import ADR_DIR, validate_adr
from tests.agent_evals.full_loop_journeys.schema import load_dataset, validate_dataset

ROOT = Path(__file__).resolve().parents[2]


def test_full_loop_dataset_has_65_mappable_privacy_safe_cases():
    summary = validate_dataset(load_dataset())
    assert summary["total"] == 65
    assert len(summary["categories"]) == 13
    assert set(summary["categories"].values()) == {5}


def test_legacy_contract_snapshots_match_registered_routes():
    snapshot_path = (
        ROOT / "tests" / "agent_evals" / "full_loop_journeys" / "legacy_contract_snapshots.v1.json"
    )
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    route_keys = {
        (method, route.path)
        for router in (pipeline_router, chat_router, autonomous_router)
        for route in router.routes
        for method in (route.methods or set())
    }
    surfaces: set[str] = set()
    for item in payload["snapshots"]:
        assert (item["method"], item["path"]) in route_keys
        assert item["status_codes"]
        assert "retry" in item
        if item["path"].startswith("/api/pipeline"):
            surfaces.add("pipeline")
        elif item["path"].startswith("/api/autonomous"):
            surfaces.add("autonomous")
        else:
            surfaces.add("chat")
    assert surfaces == {"pipeline", "chat", "autonomous"}
    assert len(payload["write_snapshots"]) >= 4
    assert all("before" in item and "after" in item for item in payload["write_snapshots"])


def test_domain_baseline_is_candid_and_freezes_hard_gates():
    report = json.loads(
        (ROOT / "reports" / "full-loop-domain-quality-legacy.v1.json").read_text(encoding="utf-8")
    )
    assert report["legacy_chat"]["total_requests"] == 120
    assert report["paired_agent_eval"]["n_cases"] == 155
    assert "human_cross_review_pending" in report["status"]
    assert set(report["comparison_rule"]["hard_safety_gates"].values()) == {0}


def test_all_full_loop_adrs_pass_required_field_lint():
    files = sorted(ADR_DIR.glob("*.md"))
    assert files
    assert [error for path in files for error in validate_adr(path)] == []
