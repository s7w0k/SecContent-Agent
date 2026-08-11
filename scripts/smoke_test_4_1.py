"""
任务 4.1 冒烟测试 — 无需 Docker / MongoDB / DeepSeek API
验证分类器核心逻辑在本地环境中正常工作。

运行:
    python scripts/smoke_test_4_1.py
"""

import os
import sys

# 添加 backend 到 Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "backend"))


def test_category_enum():
    """验证 6 分类枚举"""
    from agent.classifier_v2 import CategoryV2

    values = CategoryV2.valid_values()
    assert len(values) == 6, f"Expected 6 categories, got {len(values)}"
    assert "爆点事件" in values
    assert "法律法规/监管动态" in values
    assert "AI技术重大进展" in values
    assert "国内外竞品信息" in values
    assert "运营商/行业事件" in values
    assert "学术/会展/高校" in values
    print("  [PASS] CategoryV2: 6 分类枚举正确")

    pr = CategoryV2.pr_eligible()
    assert len(pr) == 3
    assert "爆点事件" in pr
    print(f"  [PASS] PR 候选类别 ({len(pr)} 类): {pr}")


def test_prompt_templates():
    """验证 Prompt 模板"""
    from agent.classifier_v2 import SYSTEM_PROMPT, ClassifierV2

    # System Prompt 包含所有类别
    for cat in ["爆点事件", "法律法规/监管动态", "AI技术重大进展",
                 "国内外竞品信息", "运营商/行业事件", "学术/会展/高校"]:
        assert cat in SYSTEM_PROMPT, f"SYSTEM_PROMPT missing: {cat}"
    print("  [PASS] SYSTEM_PROMPT: 6 类定义完整")

    # User Prompt 构建
    article = {"title": "测试标题", "source": "测试来源", "summary": "测试摘要"}
    prompt = ClassifierV2._build_user_prompt(article)
    assert "测试标题" in prompt
    assert "测试来源" in prompt
    print("  [PASS] User Prompt: 正确注入文章信息")


def test_json_parsing():
    """验证 JSON 解析"""
    from agent.classifier_v2 import ClassifierV2

    # 代码块格式
    r = ClassifierV2._parse_response(
        '```json\n{"category": "爆点事件", "confidence": 90, "reason": "测试"}\n```'
    )
    assert r["category"] == "爆点事件"
    assert r["confidence"] == 90

    # 纯 JSON
    r = ClassifierV2._parse_response(
        '{"category": "AI技术重大进展", "confidence": 75, "reason": "新模型"}'
    )
    assert r["category"] == "AI技术重大进展"

    # 文本中的 JSON
    r = ClassifierV2._parse_response(
        '分析结果：{"category": "运营商/行业事件", "confidence": 60, "reason": "x"}。以上。'
    )
    assert r["category"] == "运营商/行业事件"

    # 非法输入抛异常
    try:
        ClassifierV2._parse_response("No JSON here")
        raise AssertionError("Should have raised")
    except ValueError:
        pass
    print("  [PASS] JSON 解析: 3 种格式 + 异常处理正确")


def test_result_validation():
    """验证结果校验"""
    from agent.classifier_v2 import ClassifierV2

    # 合法类别
    r = ClassifierV2._validate_and_fix(
        {"category": "爆点事件", "confidence": 85, "reason": "ok"}
    )
    assert r["category"] == "爆点事件"
    assert r["confidence"] == 85

    # 非法类别 -> 降级
    r = ClassifierV2._validate_and_fix(
        {"category": "不存在的类别", "confidence": 80, "reason": "..."}
    )
    assert r["category"] == "学术/会展/高校", f"Expected fallback, got {r['category']}"

    # 置信度越界
    r = ClassifierV2._validate_and_fix(
        {"category": "国内外竞品信息", "confidence": 999, "reason": "ok"}
    )
    assert r["confidence"] == 100

    r = ClassifierV2._validate_and_fix(
        {"category": "国内外竞品信息", "confidence": -10, "reason": "ok"}
    )
    assert r["confidence"] == 0

    print("  [PASS] 结果校验: 白名单 + 范围限制 + 默认值正确")


def test_classify_result():
    """验证 ClassifyResultV2 数据类"""
    from agent.classifier_v2 import ClassifyResultV2

    # PR 候选判断
    assert ClassifyResultV2(category="爆点事件").is_pr_eligible is True
    assert ClassifyResultV2(category="法律法规/监管动态").is_pr_eligible is True
    assert ClassifyResultV2(category="AI技术重大进展").is_pr_eligible is True
    assert ClassifyResultV2(category="国内外竞品信息").is_pr_eligible is False
    assert ClassifyResultV2(category="运营商/行业事件").is_pr_eligible is False
    assert ClassifyResultV2(category="学术/会展/高校").is_pr_eligible is False

    # 降级标记
    assert ClassifyResultV2(fallback=True).is_fallback is True
    assert ClassifyResultV2(fallback=False).is_fallback is False

    # to_dict 输出
    d = ClassifyResultV2(
        category="爆点事件", confidence=92, reason="重大漏洞", fallback=False,
    ).to_dict()
    assert d["category_v2"] == "爆点事件"
    assert d["is_pr_eligible"] is True
    assert d["category_v2_fallback"] is False

    print("  [PASS] ClassifyResultV2: PR判断 + 降级 + 序列化正确")


def test_article_model():
    """验证 Article 模型 V2 字段"""
    from models.article import ArticleBase

    # 默认值
    a = ArticleBase(
        url_hash="d41d8cd98f00b204e9800998ecf8427e",
        title="Test",
        url="https://example.com",
        source="Test",
    )
    assert a.category_v2 == ""
    assert a.category_v2_confidence == 0
    assert a.is_pr_eligible is False

    # 赋值
    a2 = ArticleBase(
        url_hash="d41d8cd98f00b204e9800998ecf8427e",
        title="Test",
        url="https://example.com",
        source="Test",
        category_v2="爆点事件",
        category_v2_confidence=90,
        is_pr_eligible=True,
    )
    assert a2.category_v2 == "爆点事件"
    assert a2.is_pr_eligible is True

    print("  [PASS] Article 模型: category_v2 字段正常")


if __name__ == "__main__":
    print("=" * 60)
    print("  任务 4.1 冒烟测试 -- classifier_v2 核心逻辑验证")
    print("  (无需 Docker / MongoDB / DeepSeek API)")
    print("=" * 60)

    tests = [
        ("CategoryV2 枚举", test_category_enum),
        ("Prompt 模板", test_prompt_templates),
        ("JSON 解析", test_json_parsing),
        ("结果校验", test_result_validation),
        ("ClassifyResultV2", test_classify_result),
        ("Article 模型", test_article_model),
    ]

    passed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {name}: {e}")

    print(f"\n{'=' * 60}")
    print(f"  结果: {passed}/{len(tests)} 通过")
    if passed == len(tests):
        print("  [OK] 任务 4.1 核心逻辑验证通过！")
    else:
        print(f"  [ERR] {len(tests) - passed} 项失败，请检查")
    print(f"{'=' * 60}")
