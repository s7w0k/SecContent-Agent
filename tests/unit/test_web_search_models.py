"""Web search Pydantic 模型单元测试。"""

from __future__ import annotations

import pytest
from models.web_search import (
    ImportBatchStatus,
    SearchImportItem,
    SearchImportItemStatus,
    SearchImportRequest,
    SearchImportResponse,
    SearchImportSummary,
    SearchSessionResponse,
    SearchStatusResponse,
    SearchWarning,
    WebSearchRequest,
    WebSearchResponse,
    WebSearchResult,
)
from pydantic import ValidationError


class TestWebSearchRequest:
    """WebSearchRequest 校验。"""

    def test_defaults(self):
        req = WebSearchRequest(q="MCP 漏洞")
        assert req.q == "MCP 漏洞"
        assert req.categories == ["general"]
        assert req.language == "all"
        assert req.time_range is None
        assert req.safesearch == 1
        assert req.pageno == 1

    @pytest.mark.parametrize("q", ["", "a", "x" * 201])
    def test_q_length_validation(self, q):
        with pytest.raises(ValidationError):
            WebSearchRequest(q=q)

    def test_categories_defaults_to_general_when_empty(self):
        req = WebSearchRequest(q="test", categories=[])
        assert req.categories == ["general"]

    def test_categories_allows_two(self):
        req = WebSearchRequest(q="test", categories=["general", "news"])
        assert req.categories == ["general", "news"]

    def test_categories_rejects_three(self):
        with pytest.raises(ValidationError):
            WebSearchRequest(q="test", categories=["general", "news", "general"])

    def test_categories_rejects_unknown(self):
        with pytest.raises(ValidationError):
            WebSearchRequest(q="test", categories=["videos"])

    @pytest.mark.parametrize("tr", ["day", "month", "year"])
    def test_time_range_valid(self, tr):
        req = WebSearchRequest(q="test", time_range=tr)
        assert req.time_range == tr

    def test_time_range_invalid(self):
        with pytest.raises(ValidationError):
            WebSearchRequest(q="test", time_range="week")

    @pytest.mark.parametrize("ss", [-1, 3])
    def test_safesearch_out_of_range(self, ss):
        with pytest.raises(ValidationError):
            WebSearchRequest(q="test", safesearch=ss)

    @pytest.mark.parametrize("pn", [0, 11])
    def test_pageno_out_of_range(self, pn):
        with pytest.raises(ValidationError):
            WebSearchRequest(q="test", pageno=pn)


class TestWebSearchResult:
    """WebSearchResult 默认值。"""

    def test_defaults(self):
        result = WebSearchResult(
            result_id="res_abc",
            title="示例标题",
            url="https://example.com/article",
            display_domain="example.com",
        )
        assert result.result_id == "res_abc"
        assert result.snippet == ""
        assert result.engines == []
        assert result.category == "general"
        assert result.searxng_score is None
        assert result.is_imported is False
        assert result.article_url_hash is None
        assert result.published_at is None

    def test_full_payload(self):
        result = WebSearchResult(
            result_id="res_abc",
            title="标题",
            url="https://example.com/article",
            display_domain="example.com",
            snippet="摘要",
            published_at="2026-07-28",
            engines=["google", "bing"],
            category="news",
            searxng_score=1.5,
            is_imported=True,
            article_url_hash="d41d8cd98f00b204e9800998ecf8427e",
        )
        assert result.engines == ["google", "bing"]
        assert result.searxng_score == 1.5
        assert result.is_imported is True


class TestSearchImportRequest:
    """SearchImportRequest 校验。"""

    def test_valid(self):
        req = SearchImportRequest(search_id="srch_1", result_ids=["res_a", "res_b"])
        assert req.search_id == "srch_1"
        assert req.result_ids == ["res_a", "res_b"]

    def test_requires_search_id(self):
        with pytest.raises(ValidationError):
            SearchImportRequest(search_id="", result_ids=["res_a"])

    def test_requires_at_least_one_result_id(self):
        with pytest.raises(ValidationError):
            SearchImportRequest(search_id="srch_1", result_ids=[])

    def test_rejects_more_than_twenty(self):
        with pytest.raises(ValidationError):
            SearchImportRequest(
                search_id="srch_1",
                result_ids=[f"res_{i}" for i in range(21)],
            )


class TestSearchImportSummary:
    """SearchImportSummary 默认值。"""

    def test_defaults(self):
        summary = SearchImportSummary(requested=5)
        assert summary.requested == 5
        assert summary.imported == 0
        assert summary.duplicate == 0
        assert summary.failed == 0
        assert summary.enrichment_queued == 0


class TestSearchStatusResponse:
    """SearchStatusResponse。"""

    def test_fields(self):
        status = SearchStatusResponse(
            enabled=True,
            available=True,
            allowed_categories=["general", "news"],
            allowed_languages=["all", "zh", "en"],
            max_import_items=20,
        )
        assert status.enabled is True
        assert status.available is True
        assert status.allowed_categories == ["general", "news"]
        assert status.allowed_languages == ["all", "zh", "en"]
        assert status.max_import_items == 20


class TestSearchWarningAndEnums:
    """SearchWarning 与枚举。"""

    def test_search_warning_defaults(self):
        w = SearchWarning(code="PARTIAL", message="部分引擎超时")
        assert w.code == "PARTIAL"
        assert w.message == "部分引擎超时"
        assert w.count == 0

    def test_search_import_item_status_enum(self):
        assert {s.value for s in SearchImportItemStatus} == {
            "imported",
            "duplicate",
            "invalid_url",
            "failed",
        }

    def test_import_batch_status_enum(self):
        assert {s.value for s in ImportBatchStatus} == {
            "processing",
            "completed",
            "partial",
            "failed",
        }

    def test_search_import_item_defaults(self):
        item = SearchImportItem(result_id="res_a", status=SearchImportItemStatus.IMPORTED)
        assert item.article_url_hash is None
        assert item.message == ""

    def test_search_import_response(self):
        resp = SearchImportResponse(
            batch_id="batch_1",
            summary=SearchImportSummary(requested=2, imported=1, duplicate=1),
            items=[
                SearchImportItem(
                    result_id="res_a",
                    status=SearchImportItemStatus.IMPORTED,
                    article_url_hash="hash_a",
                ),
                SearchImportItem(
                    result_id="res_b",
                    status=SearchImportItemStatus.DUPLICATE,
                ),
            ],
        )
        assert resp.batch_id == "batch_1"
        assert resp.summary.imported == 1
        assert len(resp.items) == 2

    def test_web_search_response_and_session(self):
        result = WebSearchResult(
            result_id="res_abc",
            title="标题",
            url="https://example.com",
            display_domain="example.com",
        )
        for model_cls in (WebSearchResponse, SearchSessionResponse):
            resp = model_cls(
                search_id="srch_1",
                query={"q": "test"},
                results=[result],
                page=1,
                has_more=False,
                warnings=[],
                expires_at="2026-07-28T12:00:00Z",
            )
            assert resp.search_id == "srch_1"
            assert resp.results[0].result_id == "res_abc"
            assert resp.has_more is False
            assert resp.warnings == []
