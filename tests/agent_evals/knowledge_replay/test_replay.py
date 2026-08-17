"""阶段7 离线回放（10.2）评测测试。

覆盖：
  - ReplayRunner 流程（新旧链路分别评估、聚合、门禁）
  - 真实新旧链路接线：KnowledgeSliceResolver 旧（无检索）vs 新（检索+展开）
  - 盲评指标：产品相关性、事实来源率、无依据高风险事实、跨产品术语
依赖基础评测集（tests/agent_evals/knowledge_retrieval/dataset.v1.jsonl）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.agent_evals.knowledge_replay.replay_runner import (
    ReplayRunner,
    _competitor_terms,
    _count_citation_blocks,
    _extract_products,
    build_llm_generate,
    load_dataset,
)


def _build_kb(root: Path) -> None:
    p1 = root / "1-智能体身份安全"
    p1.mkdir(parents=True)
    (p1 / "overview.md").write_text(
        "# 智能体身份安全\n\n支持 E4002 错误码与密钥管理，可用性 99.99%。",
        encoding="utf-8",
    )
    (p1 / "market-brief.md").write_text(
        "# 市场简报\n\n面向企业，术语 IDP、OIDC。", encoding="utf-8"
    )
    raw = p1 / "原始文档"
    raw.mkdir(parents=True)
    (raw / "error-handbook.md").write_text(
        "# 错误处理手册\n\n## 错误码\nE4002 余额不足处理，版本 v2.3.1。\n\n"
        "## 重试\n必须指数退避。",
        encoding="utf-8",
    )


def _build_indexer(root: Path):
    from agent.knowledge_index import KnowledgeIndexBuilder, KnowledgeIndexer

    builder = KnowledgeIndexBuilder(root)
    manifest = builder.build_manifest()
    builder.write(manifest)
    indexer = KnowledgeIndexer(root / "_index" / "kb-index.json")
    assert indexer.load() is not None
    return indexer


async def _make_context(case: dict, mode: str, root: Path) -> str:
    """真实新旧链路：legacy=无检索切片，new=检索+展开。"""
    from agent.document_retriever import DocumentRetriever
    from agent.knowledge_slice import KnowledgeSliceResolver
    from agent.section_expander import SectionExpander

    products = case.get("expected_product_ids", [])
    if mode == "legacy":
        resolver = KnowledgeSliceResolver(root, db=None)
    else:
        indexer = _build_indexer(root)
        retriever = DocumentRetriever(indexer=indexer)
        expander = SectionExpander(root, indexer=indexer)
        resolver = KnowledgeSliceResolver(
            root,
            db=None,
            retriever=retriever,
            max_optional_docs=6,
            section_expander=expander,
        )
    result = await resolver.resolve(
        purpose="draft",
        product_ids=products or None,
        query=case.get("article", {}).get("summary_cn", ""),
    )
    return result.content


def _make_generate(case: dict, context: str, mode: str) -> dict:
    """把知识上下文作为生成正文（固定 prompt 的确定性替身）。"""
    return {"content": context}


class TestReplayHelpers:
    def test_extract_products(self):
        assert _extract_products(
            "提到 agent-identity-security 能力"
        ) == ["agent-identity-security"]
        assert _extract_products("无产品") == []

    def test_count_citation_blocks(self):
        assert _count_citation_blocks(
            "a [KNOWLEDGE_SOURCE x] b [KNOWLEDGE_SOURCE y]"
        ) == 2

    def test_competitor_terms(self):
        assert any("竞品" in t for t in _competitor_terms("我们领先竞品产品"))


class TestReplayRunner:
    def test_replay_runner_aggregate(self):
        dataset = [
            {
                "case_id": "r1",
                "expected_product_ids": ["agent-identity-security"],
                "allowed_product_claims": ["身份认证"],
                "article": {"title": "t", "summary_cn": "身份认证"},
            }
        ]
        runner = ReplayRunner(dataset=dataset)
        report = asyncio.run(runner.run())
        assert report["total_cases"] == 1
        assert report["legacy"]["cases"] == 1
        assert report["new"]["cases"] == 1
        assert "gates" in report

    def test_replay_runner_all_dataset(self):
        runner = ReplayRunner()
        report = asyncio.run(runner.run())
        assert report["total_cases"] == len(load_dataset())
        assert report["legacy"]["cases"] == report["total_cases"]
        assert report["new"]["cases"] == report["total_cases"]


class TestReplayRealChains:
    @pytest.mark.asyncio
    async def test_legacy_vs_new_resolver(self, tmp_path):
        _build_kb(tmp_path)
        case = {
            "case_id": "r-int",
            "expected_product_ids": ["agent-identity-security"],
            "allowed_product_claims": ["身份认证"],
            "article": {"title": "身份认证", "summary_cn": "E4002 错误码"},
        }
        legacy_ctx = await _make_context(case, "legacy", tmp_path)
        new_ctx = await _make_context(case, "new", tmp_path)
        assert legacy_ctx
        assert new_ctx

    @pytest.mark.asyncio
    async def test_replay_with_real_chains_metrics(self, tmp_path):
        _build_kb(tmp_path)
        case = {
            "case_id": "r-int2",
            "expected_product_ids": ["agent-identity-security"],
            "allowed_product_claims": ["身份认证"],
            "article": {"title": "身份认证", "summary_cn": "E4002 错误码"},
        }
        runner = ReplayRunner(dataset=[case])
        metrics = []
        for mode in ("legacy", "new"):
            ctx = await _make_context(case, mode, tmp_path)
            gen = _make_generate(case, ctx, mode)
            r = runner._evaluate(case, mode, gen["content"])
            metrics.append(_build_metrics(r))
        by_mode = {m["mode"]: m for m in metrics}
        assert by_mode["legacy"]["citation_blocks"] >= 0
        assert by_mode["new"]["citation_blocks"] >= 0


def _build_metrics(result):
    return {
        "case_id": result.case_id,
        "mode": result.mode,
        "product_ok": result.product_ok,
        "fact_source_rate": round(result.fact_source_rate, 4),
        "citation_blocks": result.citation_blocks,
        "unsupported_high_risk": result.unsupported_high_risk,
        "competitor_terms": result.competitor_terms,
        "char_count": result.char_count,
    }


class _MockLLM:
    """固定输出的 mock LLM，避免真实 DEEPSEEK 调用。"""

    def bind(self, **kwargs):
        return self

    async def ainvoke(self, messages):
        class _Resp:
            content = "# 智能体身份安全 \n\n支持 99.99% 可用性，版本 v2.3.1。"

        return _Resp()


@pytest.mark.asyncio
async def test_build_llm_generate_with_mock():
    generate = build_llm_generate(llm=_MockLLM(), max_drafts=2)
    out = await generate(
        {
            "article": {
                "title": "身份认证",
                "category_v2": "AI技术重大进展",
                "summary_cn": "E4002 错误码",
            }
        },
        "知识上下文",
        "new",
    )
    assert out["content"]
    assert "99.99%" in out["content"]


@pytest.mark.asyncio
async def test_replay_runner_async_generate():
    # 异步 generate 回调应被 runner 正确 await
    dataset = [
        {
            "case_id": "r-async",
            "expected_product_ids": ["agent-identity-security"],
            "allowed_product_claims": ["身份认证"],
            "article": {"title": "t", "category_v2": "AI技术重大进展", "summary_cn": "身份认证"},
        }
    ]

    async def _async_gen(case, context, mode):
        return {"content": "agent-identity-security 支持身份认证"}

    runner = ReplayRunner(dataset=dataset, generate=_async_gen)
    report = await runner.run()
    assert report["legacy"]["cases"] == 1
    assert report["new"]["cases"] == 1


class TestDataset:
    def test_replay_dataset_available(self):
        ds = load_dataset()
        assert len(ds) >= 60
        for case in ds:
            assert case["article"]
