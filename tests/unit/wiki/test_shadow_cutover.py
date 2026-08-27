"""Final Plan PR-C：Shadow/Canary/Cutover 测试（§7）。

覆盖：
  - Canary 稳定 hash 分桶（同 seed 同桶，可复现）
  - percent 边界与 5%→100% 渐进切流
  - Runtime 把 llm 装配进 Navigator（WIKI_NAVIGATOR_LLM_ENABLED=true/false）
  - strict Wiki 的 scoring 端不调用 legacy 提示词（§7.12 硬测试）
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent.wiki.cutover import in_canary, stable_bucket
from agent.wiki.provider import WikiKnowledgeProvider
from agent.wiki.runtime_factory import build_knowledge_runtime

from tests.unit.wiki.test_runtime_factory import _seed_wiki


class _Settings:
    def __init__(
        self,
        *,
        backend: str,
        wiki_root: str,
        base_dir: str,
        navigator_llm: bool = False,
    ):
        self.KNOWLEDGE_BACKEND = backend
        self.WIKI_ROOT_DIR = wiki_root
        self.KNOWLEDGE_BASE_DIR = base_dir
        self.WIKI_NAVIGATOR_LLM_ENABLED = navigator_llm
        self.WIKI_REQUIRE_SOURCE_GROUNDING = True


# ── Canary（§7.6）──────────────────────────────────────────


def test_canary_bucket_is_stable_per_seed():
    assert stable_bucket("user-1") == stable_bucket("user-1")
    assert stable_bucket("trace-999") == stable_bucket("trace-999")
    assert stable_bucket("") == stable_bucket("")


def test_canary_bucket_in_range():
    for seed in ["", "user-a", "trace-123", "x" * 100]:
        assert 0 <= stable_bucket(seed) < 100


def test_canary_percent_boundaries():
    assert in_canary("any", 0) is False
    assert in_canary("any", 100) is True


def test_canary_rollout_monotonic():
    """同 seed 在 percent 增大时不会"先命中再退出"。"""
    for seed in ["", "u1", "u2", "trace-1"]:
        assert in_canary(seed, 20) == in_canary(seed, 20)
        assert not in_canary(seed, 20) or in_canary(seed, 50)


def test_canary_5_percent_is_subset_of_50():
    """5% 命中集合 ⊂ 50% 命中集合（渐进切流的单调性）。"""
    seeds = [f"user-{i}" for i in range(200)]
    at5 = {s for s in seeds if in_canary(s, 5)}
    at50 = {s for s in seeds if in_canary(s, 50)}
    assert at5 <= at50
    assert len(at5) < len(at50)


# ── Runtime LLM 装配（§4.7 / §7.11）────────────────────────


def test_runtime_wires_llm_into_navigator_when_enabled(tmp_path: Path) -> None:
    source_root = tmp_path / "docs"
    wiki_root = source_root / "_wiki"
    _seed_wiki(tmp_path, source_root, wiki_root)
    settings = _Settings(
        backend="wiki", wiki_root=str(wiki_root), base_dir=str(source_root), navigator_llm=True
    )
    rt = build_knowledge_runtime(settings, llm=object())
    assert isinstance(rt.provider, WikiKnowledgeProvider)
    assert rt.provider.navigator.llm_enabled is True


def test_runtime_disables_llm_when_disabled(tmp_path: Path) -> None:
    source_root = tmp_path / "docs"
    wiki_root = source_root / "_wiki"
    _seed_wiki(tmp_path, source_root, wiki_root)
    settings = _Settings(
        backend="wiki", wiki_root=str(wiki_root), base_dir=str(source_root), navigator_llm=False
    )
    rt = build_knowledge_runtime(settings, llm=object())
    assert rt.provider.navigator.llm_enabled is False


# ── strict Wiki scoring：不得构建 legacy 提示词（§7.12）────


class _NoLegacyCall:
    def __init__(self):
        self.calls = 0

    async def invoke_structured(self, **kwargs):
        self.calls += 1
        raise AssertionError("strict wiki 不应调用 legacy 提示词")


def test_scoring_strict_wiki_never_builds_legacy_prompt(tmp_path: Path) -> None:
    """证据不足 → NO_SCORE；即使意外落进评分分支，legacy 大段上下文也绝不构建（§7.12）。"""
    from agent.scorer_v2 import ScoringAgentV2
    from agent.wiki.evidence import EvidenceBundle

    class _FakeKnowledge:
        def as_scoring_prompt(self):
            return "legacy 常识"

    class _FakeLLM:
        temperature = 0.1

    class _Provider:
        mode = "wiki"

        async def collect_evidence(self, request):
            return EvidenceBundle(
                status="INSUFFICIENT_EVIDENCE",
                task_type="score",
                query="q",
                product_ids=["agent_identity"],
                evidence=[],  # 无经过验证的证据
                coverage=0.0,
                confidence=0.0,
                visited_pages=["capability.x"],
                wiki_version="v",
            )

    scorer = ScoringAgentV2(llm=_FakeLLM(), knowledge=_FakeKnowledge(), db=None,
                            knowledge_provider=_Provider())
    scorer.llm_wrapper = _NoLegacyCall()

    result = asyncio.run(
        scorer._score_single_product(
            {"title": "t", "summary": "s", "category_v2": "身份安全"},
            product_id="agent_identity",
            product_name="身份",
        )
    )
    assert scorer.llm_wrapper.calls == 0  # 硬断言：未调用任何 legacy/证据提示词
    assert result["_no_score"] is True
    assert result["status"] == "INSUFFICIENT_PRODUCT_EVIDENCE"
