"""Phase 6 / PR-12：Entity Resolver + 中文检索 + 删除全产品兜底（G-07/G-09，§9）。"""

from __future__ import annotations

from agent.wiki.index import (
    WikiIndex,
    WikiIndexManifest,
    WikiPageIndex,
    build_search_terms,
)
from agent.wiki.navigator import WikiNavigator
from agent.wiki.resolver import (
    EntityResolver,
    EntityStatus,
    MatchType,
    normalize_text,
)


def _page_index(
    page_id: str,
    *,
    title: str | None = None,
    product_id: str | None = None,
    aliases: list[str] | None = None,
    summary: str = "",
) -> WikiPageIndex:
    return WikiPageIndex(
        page_id=page_id,
        title=title or page_id,
        page_type=page_id.split(".", 1)[0],
        product_id=product_id,
        aliases=aliases or [],
        summary=summary,
        search_terms=build_search_terms(title or page_id, aliases or [], summary),
    )


def _resolver(pages: list[WikiPageIndex]) -> EntityResolver:
    manifest = WikiIndexManifest(wiki_version="v", page_count=len(pages), pages=pages)
    return EntityResolver(index=WikiIndex(manifest))


# ── Resolution Pipeline（§9.1）────────────────────────────


def test_resolve_exact_product_id():
    r = _resolver([_page_index("product.alpha", title="Alpha 平台", product_id="alpha")])
    out = r.resolve("alpha")
    assert out.status == EntityStatus.RESOLVED
    assert out.top is not None
    assert out.top.page_id == "product.alpha"
    assert out.top.score == 1.0
    assert out.top.match_type == MatchType.PRODUCT_ID.value


def test_resolve_exact_canonical_name():
    r = _resolver([_page_index("product.alpha", title="Alpha 平台", product_id="alpha")])
    out = r.resolve("Alpha 平台")
    assert out.status == EntityStatus.RESOLVED
    assert out.top.match_type == MatchType.CANONICAL.value


def test_resolve_exact_alias():
    r = _resolver(
        [_page_index("product.alpha", product_id="alpha", aliases=["authplatform", "AAP"])]
    )
    out = r.resolve("AAP")
    assert out.status == EntityStatus.RESOLVED
    assert out.top.match_type == MatchType.ALIAS.value


def test_resolve_normalized_alias_fullwidth():
    # NFKC：全角 ＡＡＰ → 半角 aap（§9.4 中文/全角归一化）
    r = _resolver([_page_index("product.alpha", product_id="alpha", aliases=["AAP"])])
    assert normalize_text("ＡＡＰ") == "aap"
    out = r.resolve("ＡＡＰ")
    assert out.status == EntityStatus.RESOLVED
    assert out.top.match_type == MatchType.NORMALIZED.value


def test_resolve_fuzzy_alias():
    r = _resolver([_page_index("product.auth", product_id="auth", aliases=["identity_auth_api"])])
    out = r.resolve("identity_auth_ap")
    assert out.status == EntityStatus.RESOLVED
    assert out.top.match_type == MatchType.FUZZY.value


def test_resolve_unknown_entity():
    r = _resolver([_page_index("product.alpha", product_id="alpha")])
    out = r.resolve("不存在的东西xyz")
    assert out.status == EntityStatus.UNKNOWN_ENTITY
    assert out.candidates == []


# ── Ambiguity Contract（§9.2）─────────────────────────────


def test_contract_ambiguous_when_two_entities_share_top_score():
    # 两个不同 entity 同时精确命中同一 alias → top1-top2=0 < margin
    r = _resolver(
        [
            _page_index("product.alpha", product_id="alpha", aliases=["agent"]),
            _page_index("product.beta", product_id="beta", aliases=["agent"]),
        ]
    )
    out = r.resolve("agent")
    assert out.status == EntityStatus.AMBIGUOUS_ENTITY
    assert len(out.candidates) == 2


def test_contract_resolved_when_single_dominant():
    r = _resolver(
        [
            _page_index("product.alpha", product_id="alpha", aliases=["agent"]),
            _page_index("product.beta", product_id="beta", aliases=[]),
        ]
    )
    out = r.resolve("agent")
    assert out.status == EntityStatus.RESOLVED


def test_product_id_hits_collapse_to_one_candidate():
    # 同一 product 下多个子页 → 折叠为一个 canonical 候选，不误判歧义
    pages = [
        _page_index("product.alpha", product_id="alpha"),
        _page_index("product.alpha.capability.auth", product_id="alpha", title="身份认证"),
        _page_index("product.alpha.scenario.spoofing", product_id="alpha", title="冒用"),
    ]
    out = _resolver(pages).resolve("alpha")
    assert out.status == EntityStatus.RESOLVED
    assert out.top.page_id == "product.alpha"


# ── Query 边界（§9.5）────────────────────────────────────


def test_query_edge_cases_do_not_crash():
    r = _resolver([_page_index("product.alpha", product_id="alpha", aliases=["AAP"])])
    for q in ["", "a", "A" * 500, "😀🚀", "ＡＡＰ", "agent.AAP", "αβε", "select*from"]:
        out = r.resolve(q)
        assert out.status in {
            EntityStatus.RESOLVED,
            EntityStatus.AMBIGUOUS_ENTITY,
            EntityStatus.UNKNOWN_ENTITY,
        }
    # 空查询必须 UNKNOWN_ENTITY
    assert r.resolve("").status == EntityStatus.UNKNOWN_ENTITY


# ── 中文检索 search_terms（§9.4）─────────────────────────


def test_build_search_terms_cjk_ngram():
    terms = build_search_terms("单点登录", ["SSO"], "支持单点登录能力")
    assert "单点" in terms
    assert "点登" in terms
    assert "登录" in terms
    assert "sso" in terms


def test_index_search_matches_chinese_via_terms():
    manifest = WikiIndexManifest(
        wiki_version="v",
        page_count=1,
        pages=[
            _page_index(
                "product.alpha.capability.auth",
                title="单点登录",
                product_id="alpha",
                aliases=[],
                summary="支持单点登录能力的统一登录",
            )
        ],
    )
    idx = WikiIndex(manifest)
    hits = idx.search("点登录")
    assert len(hits) == 1


# ── 删除 All-product Fallback（§9.3）──────────────────────


async def test_navigator_unknown_entity_does_not_scan_all_products(store):
    # 有 product 页，但解析不到实体 → 不得打开任何 product 页
    store.write_page(_make_nav_page("product.alpha", "alpha"))
    store.write_page(_make_nav_page("product.beta", "beta"))
    nav = WikiNavigator(store, resolver=EntityResolver(store=store))
    outcome = await nav.navigate("某个不存在的产品xyz", task_type="score")
    assert outcome.visited == []
    assert outcome.stop_reason == "UNKNOWN_ENTITY"


async def test_navigator_resolves_via_bounded_search(store):
    store.write_page(_make_nav_page("product.alpha", "alpha", aliases=["auth"]))
    store.write_page(_make_nav_page("product.beta", "beta"))
    nav = WikiNavigator(store, resolver=EntityResolver(store=store))
    outcome = await nav.navigate("auth", task_type="score")
    assert "product.alpha" in outcome.visited
    assert "product.beta" not in outcome.visited


def _make_nav_page(page_id: str, product_id: str, aliases: list[str] | None = None):
    from helpers import make_page

    return make_page(page_id, product_id=product_id, aliases=aliases or [])
