"""任务 11.2：稿件内容与宣传话术审核器测试。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from agent.draft_reviewer import DraftReviewer, scan_keyword_candidates


def _response(issues: list[dict], summary: str = "检查完成") -> MagicMock:
    return MagicMock(content=json.dumps({"summary": summary, "issues": issues}, ensure_ascii=False))


def _issue(category: str, severity: str, quote: str, reason: str = "存在问题") -> dict:
    return {
        "category": category,
        "severity": severity,
        "quote": quote,
        "reason": reason,
        "suggestion": "按原文准确、审慎地改写",
        "suggested_rewrite": None,
    }


def _reviewer(response: MagicMock) -> DraftReviewer:
    llm = MagicMock()
    llm.bind = MagicMock(return_value=llm)
    llm.ainvoke = AsyncMock(return_value=response)
    return DraftReviewer(llm)


@pytest.mark.asyncio
async def test_reviews_fact_mismatch_and_unsupported_claim():
    draft_content = "张三表示漏洞已造成1000万用户损失。该产品检测率达到99%。"
    reviewer = _reviewer(
        _response(
            [
                _issue("fact_mismatch", "high", "张三表示漏洞已造成1000万用户损失。"),
                _issue("unsupported_claim", "medium", "该产品检测率达到99%。"),
            ]
        )
    )

    result = await reviewer.review(
        {
            "title": "漏洞消息",
            "content_md": "李四表示漏洞可能影响部分用户，原文未提供检测率。",
        },
        {"title": "稿件", "content_md": draft_content},
    )

    assert result.status == "completed"
    assert [issue.category for issue in result.issues] == ["fact_mismatch", "unsupported_claim"]
    assert result.counts == {"high": 1, "medium": 1, "low": 0}


@pytest.mark.asyncio
async def test_reviews_absolute_comparison_disparagement_guarantee_and_unsupported_data():
    sentences = [
        "我们是业内第一，也是行业唯一选择。",
        "产品比某厂商更强，并能碾压竞品。",
        "平台提供100%安全和零风险保障。",
        "实测性能提升80%。",
    ]
    reviewer = _reviewer(
        _response(
            [
                _issue("absolute_claim", "medium", sentences[0]),
                _issue("competitor_comparison", "medium", sentences[1]),
                _issue("competitor_disparagement", "high", sentences[1]),
                _issue("guarantee_claim", "high", sentences[2]),
                _issue("unsupported_data", "medium", sentences[3]),
            ]
        )
    )

    result = await reviewer.review(
        {"title": "原文", "content_md": "原文仅介绍产品上线，未提供排名或测试数据。"},
        {"title": "稿件", "content_md": "".join(sentences)},
    )

    assert result.counts == {"high": 2, "medium": 3, "low": 0}
    assert [issue.severity for issue in result.issues] == [
        "high",
        "high",
        "medium",
        "medium",
        "medium",
    ]


def test_keyword_scan_returns_candidates_and_marks_disparagement():
    candidates = scan_keyword_candidates(
        "业内第一且遥遥领先。比某产品更强。可以碾压竞品。实现100%安全、零风险。"
    )

    categories = {item["category"] for item in candidates}
    assert "absolute_claim" in categories
    assert "competitor_comparison" in categories
    assert "competitor_disparagement" in categories
    assert "guarantee_claim" in categories


@pytest.mark.asyncio
async def test_no_problem_returns_empty_issues():
    reviewer = _reviewer(_response([], "未发现需要修改的问题"))

    result = await reviewer.review(
        {"title": "原文", "content_md": "产品新增风险识别功能。"},
        {"title": "稿件", "content_md": "产品新增风险识别功能。"},
    )

    assert result.status == "completed"
    assert result.issues == []
    assert sum(result.counts.values()) == 0


@pytest.mark.asyncio
async def test_missing_source_only_checks_wording_and_marks_partial():
    sentence = "该方案可以彻底杜绝所有攻击。"
    reviewer = _reviewer(_response([_issue("guarantee_claim", "high", sentence)]))

    result = await reviewer.review(
        {"title": "原文", "summary_cn": "只有摘要"},
        {"title": "稿件", "content_md": sentence},
    )

    assert result.status == "partial"
    assert result.fact_check_available is False
    assert result.issues[0].category == "guarantee_claim"
    assert "事实检查不完整" in result.summary


@pytest.mark.asyncio
async def test_invalid_json_retries_then_returns_rule_fallback():
    llm = MagicMock()
    llm.bind = MagicMock(return_value=llm)
    llm.ainvoke = AsyncMock(side_effect=[MagicMock(content="bad"), MagicMock(content="still bad")])
    reviewer = DraftReviewer(llm, max_retries=1)

    result = await reviewer.review(
        {"title": "原文", "content_md": "产品发布。"},
        {"title": "稿件", "content_md": "我们是业内第一。"},
    )

    assert llm.ainvoke.await_count == 2
    assert result.status == "partial"
    assert result.error == "review response is not valid JSON"
    assert result.issues[0].category == "absolute_claim"


@pytest.mark.asyncio
async def test_review_parses_json_wrapped_in_code_block():
    """LLM 返回 ```json 代码块包裹的 JSON 时应正确解析。"""
    draft_content = "我们是业内第一。"
    payload = json.dumps(
        {"summary": "检查完成", "issues": [_issue("absolute_claim", "medium", draft_content)]},
        ensure_ascii=False,
    )
    reviewer = _reviewer(MagicMock(content=f"```json\n{payload}\n```"))

    result = await reviewer.review(
        {"title": "原文", "content_md": "产品发布。"},
        {"title": "稿件", "content_md": draft_content},
    )

    assert result.status == "completed"
    assert result.issues[0].category == "absolute_claim"


@pytest.mark.asyncio
async def test_review_parses_json_with_surrounding_text():
    """LLM 返回带前后说明文字的 JSON 时应正确解析。"""
    draft_content = "我们是业内第一。"
    payload = json.dumps(
        {"summary": "检查完成", "issues": [_issue("absolute_claim", "medium", draft_content)]},
        ensure_ascii=False,
    )
    reviewer = _reviewer(MagicMock(content=f"好的，以下是检查结果：\n{payload}\n请参考以上分析。"))

    result = await reviewer.review(
        {"title": "原文", "content_md": "产品发布。"},
        {"title": "稿件", "content_md": draft_content},
    )

    assert result.status == "completed"
    assert result.issues[0].category == "absolute_claim"


@pytest.mark.asyncio
async def test_review_parses_json_with_trailing_commas_and_single_quotes():
    """LLM 返回含尾逗号和单引号的 JSON 时应修复后正确解析。"""
    draft_content = "我们是业内第一。"
    # 故意包含尾逗号和单引号
    raw = """{
        'summary': '检查完成',
        'issues': [
            {
                'issue_id': 'issue-001',
                'category': 'absolute_claim',
                'severity': 'medium',
                'quote': '我们是业内第一。',
                'reason': '绝对化用语',
                'suggestion': '修改',
                'suggested_rewrite': '我们在领域内有优势',
            },
        ],
    }"""
    reviewer = _reviewer(MagicMock(content=raw))

    result = await reviewer.review(
        {"title": "原文", "content_md": "产品发布。"},
        {"title": "稿件", "content_md": draft_content},
    )

    assert result.status == "completed"
    assert result.issues[0].category == "absolute_claim"


@pytest.mark.asyncio
async def test_timeout_without_rule_candidate_returns_failed():
    async def timeout(_messages):
        await asyncio.sleep(0.05)

    llm = MagicMock()
    llm.bind = MagicMock(return_value=llm)
    llm.ainvoke = AsyncMock(side_effect=timeout)
    reviewer = DraftReviewer(llm, timeout_seconds=0.001, max_retries=0)

    result = await reviewer.review(
        {"title": "原文", "content_md": "产品发布。"},
        {"title": "稿件", "content_md": "产品发布。"},
    )

    assert result.status == "failed"
    assert result.issues == []
    assert result.error is not None


@pytest.mark.asyncio
async def test_duplicate_issues_are_merged_and_missing_issue_id_is_filled():
    sentence = "该能力存在歧义。"
    reviewer = _reviewer(
        _response(
            [
                _issue("ambiguous_expression", "low", sentence),
                _issue("ambiguous_expression", "medium", sentence),
            ]
        )
    )

    result = await reviewer.review(
        {"title": "原文", "content_md": "原文。"},
        {"title": "稿件", "content_md": sentence},
    )

    assert len(result.issues) == 1
    assert result.issues[0].severity == "medium"
    assert result.issues[0].issue_id == "issue-001"
