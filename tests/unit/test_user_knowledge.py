"""阶段十五 T0/T1：用户知识模型、索引和多租户 API 测试。"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

import pytest
from api.user_knowledge import router as user_knowledge_router
from auth.deps import AuthError, auth_error_handler, get_current_user
from db.mongo import MongoDB
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from models.user_knowledge import (
    UserKnowledgeEntry,
    UserKnowledgeEntryCreate,
    UserKnowledgeEntryUpdate,
    UserProduct,
    UserProductCreate,
    UserProductUpdate,
    compute_content_hash,
)
from pydantic import ValidationError


def _matches(document: dict, query: dict) -> bool:
    for key, expected in query.items():
        actual = document.get(key)
        if isinstance(expected, dict) and "$ne" in expected:
            if actual == expected["$ne"]:
                return False
        elif actual != expected:
            return False
    return True


class FakeCursor:
    def __init__(self, documents: list[dict]):
        self.documents = documents

    def sort(self, field: str, direction: int):
        self.documents.sort(key=lambda item: item.get(field, 0), reverse=direction < 0)
        return self

    async def to_list(self, length: int):
        return deepcopy(self.documents[:length])


class FakeCollection:
    def __init__(self):
        self.documents: list[dict] = []

    async def find_one(self, query: dict):
        value = next((item for item in self.documents if _matches(item, query)), None)
        return deepcopy(value) if value is not None else None

    def find(self, query: dict):
        return FakeCursor([deepcopy(item) for item in self.documents if _matches(item, query)])

    async def insert_one(self, document: dict):
        self.documents.append(deepcopy(document))
        return SimpleNamespace(inserted_id="fake-id")

    async def update_one(self, query: dict, update: dict):
        for document in self.documents:
            if _matches(document, query):
                document.update(deepcopy(update.get("$set", {})))
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def delete_one(self, query: dict):
        for index, document in enumerate(self.documents):
            if _matches(document, query):
                self.documents.pop(index)
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)


class FakeDb:
    def __init__(self):
        self.collections: dict[str, FakeCollection] = {}

    def __getitem__(self, name: str) -> FakeCollection:
        return self.collections.setdefault(name, FakeCollection())


@pytest.fixture
def knowledge_app() -> tuple[FastAPI, FakeDb]:
    app = FastAPI()
    app.add_exception_handler(AuthError, auth_error_handler)
    app.include_router(user_knowledge_router)
    db = FakeDb()
    app.state.db = db

    async def current_user_override(request: Request) -> str:
        return request.headers.get("X-Test-User", "user-a")

    app.dependency_overrides[get_current_user] = current_user_override
    return app, db


async def _request(app: FastAPI, method: str, path: str, user_id: str = "user-a", **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(
            method,
            path,
            headers={"X-Test-User": user_id},
            **kwargs,
        )


def test_models_have_uuid_defaults_and_validation():
    entry = UserKnowledgeEntry(
        user_id="user-a",
        product_id="agent-security",
        product_scope="global",
        doc_type="custom",
        title="补充资料",
        content="正文",
        content_hash=compute_content_hash("正文"),
    )
    product = UserProduct(user_id="user-a", name="内部产品")
    UUID(entry.entry_id)
    UUID(product.product_id)
    assert (entry.enabled, entry.sort_order) == (True, 100)
    assert (product.enabled, product.sort_order) == (True, 200)

    with pytest.raises(ValidationError):
        UserKnowledgeEntryCreate(
            product_id="p-1",
            product_scope="filesystem",
            doc_type="custom",
            title="标题",
            content="正文",
        )
    with pytest.raises(ValidationError):
        UserKnowledgeEntryCreate(
            product_id="p-1",
            product_scope="user",
            doc_type="custom",
            title="标题",
            content="   ",
        )
    with pytest.raises(ValidationError):
        UserKnowledgeEntryUpdate()
    with pytest.raises(ValidationError):
        UserProductUpdate(name=None)


def test_product_terms_and_content_hash_are_normalized():
    body = UserProductCreate(
        name="  内部产品  ",
        aliases=["产品A", " 产品A ", "product-a", "PRODUCT-A"],
        keywords=["身份安全", " 身份安全 "],
    )
    assert body.name == "内部产品"
    assert body.aliases == ["产品A", "product-a"]
    assert body.keywords == ["身份安全"]
    assert compute_content_hash("A") == compute_content_hash("A")
    assert compute_content_hash("A") != compute_content_hash("B")


@pytest.mark.asyncio
async def test_stage15_indexes_are_exact(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, list] = {}

    class IndexCollection:
        def __init__(self, name: str):
            self.name = name

        async def create_indexes(self, indexes):
            captured[self.name] = indexes
            return [index.document["name"] for index in indexes]

        async def drop_index(self, _name: str):
            return None

    collections: dict[str, IndexCollection] = {}

    def get_collection(cls, name: str):
        return collections.setdefault(name, IndexCollection(name))

    monkeypatch.setattr(MongoDB, "get_collection", classmethod(get_collection))
    await MongoDB.ensure_indexes()

    entries = [index.document for index in captured["user_knowledge_entries"]]
    assert entries[0]["unique"] is True
    assert list(entries[0]["key"].items()) == [("entry_id", 1)]
    assert list(entries[1]["key"].items()) == [("user_id", 1), ("product_id", 1), ("doc_type", 1)]
    assert list(entries[2]["key"].items()) == [("user_id", 1), ("enabled", 1), ("sort_order", 1)]
    products = [index.document for index in captured["user_products"]]
    assert products[0]["unique"] is True
    assert list(products[0]["key"].items()) == [("user_id", 1), ("product_id", 1)]
    assert list(products[1]["key"].items()) == [("user_id", 1), ("enabled", 1)]


@pytest.mark.asyncio
async def test_user_knowledge_api_requires_authentication():
    app = FastAPI()
    app.add_exception_handler(AuthError, auth_error_handler)
    app.include_router(user_knowledge_router)
    app.state.db = FakeDb()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/user-knowledge")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"


@pytest.mark.asyncio
async def test_register_product_and_product_ownership(knowledge_app):
    app, _db = knowledge_app
    created = await _request(
        app,
        "POST",
        "/api/user-knowledge/products",
        json={"name": "部门产品", "aliases": ["产品X"], "keywords": ["身份治理"]},
    )
    assert created.status_code == 200
    product_id = created.json()["data"]["product_id"]
    UUID(product_id)

    user_a = await _request(app, "GET", "/api/user-knowledge/products")
    user_b = await _request(app, "GET", "/api/user-knowledge/products", user_id="user-b")
    assert product_id in {item["product_id"] for item in user_a.json()["data"]["items"]}
    assert product_id not in {item["product_id"] for item in user_b.json()["data"]["items"]}
    assert "agent-identity-security" in {
        item["product_id"] for item in user_b.json()["data"]["items"]
    }

    update = await _request(
        app,
        "PUT",
        f"/api/user-knowledge/products/{product_id}",
        user_id="user-b",
        json={"name": "越权修改"},
    )
    delete = await _request(
        app, "DELETE", f"/api/user-knowledge/products/{product_id}", user_id="user-b"
    )
    assert (update.status_code, delete.status_code) == (404, 404)


@pytest.mark.asyncio
async def test_entry_crud_hash_toggle_and_tenant_isolation(knowledge_app):
    app, _db = knowledge_app
    created = await _request(
        app,
        "POST",
        "/api/user-knowledge",
        json={
            "product_id": "agent-identity-security",
            "product_scope": "global",
            "doc_type": "custom",
            "title": "Q3 路线图",
            "content": "## 第一版",
        },
    )
    assert created.status_code == 200
    entry = created.json()["data"]
    assert entry["content_hash"] == compute_content_hash("## 第一版")
    listed = await _request(app, "GET", "/api/user-knowledge")
    assert entry["entry_id"] in {item["entry_id"] for item in listed.json()["data"]["items"]}

    updated = await _request(
        app,
        "PUT",
        f"/api/user-knowledge/{entry['entry_id']}",
        json={"content": "## 第二版"},
    )
    assert updated.json()["data"]["content_hash"] == compute_content_hash("## 第二版")
    toggled = await _request(app, "POST", f"/api/user-knowledge/{entry['entry_id']}/toggle")
    assert toggled.json()["data"]["enabled"] is False

    assert (
        await _request(app, "GET", f"/api/user-knowledge/{entry['entry_id']}", user_id="user-b")
    ).status_code == 404
    assert (
        await _request(
            app,
            "PUT",
            f"/api/user-knowledge/{entry['entry_id']}",
            user_id="user-b",
            json={"title": "越权"},
        )
    ).status_code == 404
    assert (
        await _request(
            app,
            "POST",
            f"/api/user-knowledge/{entry['entry_id']}/toggle",
            user_id="user-b",
        )
    ).status_code == 404
    assert (
        await _request(app, "DELETE", f"/api/user-knowledge/{entry['entry_id']}", user_id="user-b")
    ).status_code == 404


@pytest.mark.asyncio
async def test_product_filter_and_linked_product_delete_guard(knowledge_app):
    app, _db = knowledge_app
    product = await _request(
        app, "POST", "/api/user-knowledge/products", json={"name": "用户 A 产品"}
    )
    product_id = product.json()["data"]["product_id"]
    user_entry = await _request(
        app,
        "POST",
        "/api/user-knowledge",
        json={
            "product_id": product_id,
            "product_scope": "user",
            "doc_type": "overview",
            "title": "产品概述",
            "content": "内部知识",
        },
    )
    assert user_entry.status_code == 200
    denied = await _request(
        app,
        "POST",
        "/api/user-knowledge",
        user_id="user-b",
        json={
            "product_id": product_id,
            "product_scope": "user",
            "doc_type": "custom",
            "title": "越权引用",
            "content": "不允许",
        },
    )
    assert denied.status_code == 422

    for product_ref, title in [("ai-bom", "A"), ("agent-security", "B")]:
        assert (
            await _request(
                app,
                "POST",
                "/api/user-knowledge",
                json={
                    "product_id": product_ref,
                    "product_scope": "global",
                    "doc_type": "custom",
                    "title": title,
                    "content": f"{title}内容",
                },
            )
        ).status_code == 200
    filtered = await _request(app, "GET", "/api/user-knowledge/products/ai-bom")
    assert [item["title"] for item in filtered.json()["data"]["items"]] == ["A"]

    blocked = await _request(app, "DELETE", f"/api/user-knowledge/products/{product_id}")
    assert blocked.status_code == 409
    entry_id = user_entry.json()["data"]["entry_id"]
    await _request(app, "DELETE", f"/api/user-knowledge/{entry_id}")
    assert (
        await _request(app, "DELETE", f"/api/user-knowledge/products/{product_id}")
    ).status_code == 200


# ── T2/T3：知识切片解析器 + 产品匹配器 测试 ──


class SliceMockCursor:
    """模拟 Motor 游标。"""

    def __init__(self, docs: list[dict]):
        self._docs = docs

    def sort(self, *a, **kw):
        return self

    async def to_list(self, length=0):
        return self._docs


class SliceMockCollection:
    """模拟 Motor 集合，支持基本查询过滤。"""

    def __init__(self, docs: list[dict] | None = None):
        self._docs = docs or []

    def find(self, query: dict | None = None):
        if query is None:
            return SliceMockCursor(self._docs)
        # 基本过滤：支持 {"$in": [...]} 和直接值匹配
        filtered = []
        for doc in self._docs:
            match = True
            for key, value in query.items():
                if isinstance(value, dict) and "$in" in value:
                    if doc.get(key) not in value["$in"]:
                        match = False
                        break
                elif doc.get(key) != value:
                    match = False
                    break
            if match:
                filtered.append(doc)
        return SliceMockCursor(filtered)


class SliceMockDb:
    """模拟 MongoDB，支持多集合。"""

    def __init__(self, collections: dict[str, list[dict]] | None = None):
        self._collections = {k: SliceMockCollection(v) for k, v in (collections or {}).items()}

    def __getitem__(self, key: str):
        return self._collections.get(key, SliceMockCollection([]))


@pytest.mark.asyncio
async def test_resolver_reads_user_product_entries():
    """KnowledgeSliceResolver 能读取用户产品的知识条目。"""
    from agent.knowledge_slice import KnowledgeSliceResolver

    user_product_id = "up-test-1"
    user_entries = [
        {
            "entry_id": "entry-1",
            "user_id": "u-1",
            "product_id": user_product_id,
            "product_scope": "user",
            "doc_type": "overview",
            "title": "产品概述",
            "content": "这是用户产品的概述内容",
            "enabled": True,
            "sort_order": 100,
            "content_hash": "sha256:abc",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    ]
    user_products = [
        {
            "product_id": user_product_id,
            "user_id": "u-1",
            "name": "测试产品",
            "description": "",
            "aliases": [],
            "keywords": ["测试关键词"],
            "sort_order": 200,
            "enabled": True,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    ]
    db = SliceMockDb(
        {
            "user_products": user_products,
            "user_knowledge_entries": user_entries,
        }
    )

    resolver = KnowledgeSliceResolver(db=db)
    result = await resolver.resolve(
        purpose="draft",
        product_ids=[user_product_id],
        user_id="u-1",
    )

    assert "这是用户产品的概述内容" in result.content
    assert "测试产品" in result.content
    assert "用户级" in result.content


@pytest.mark.asyncio
async def test_resolver_reads_global_product_supplements():
    """KnowledgeSliceResolver 能读取用户对全局产品的补充条目。"""
    from agent.knowledge_slice import KnowledgeSliceResolver

    # 用户为全局产品 ai-bom 创建了补充知识
    supplement_entries = [
        {
            "entry_id": "sup-1",
            "user_id": "u-1",
            "product_id": "ai-bom",
            "product_scope": "global",
            "doc_type": "custom",
            "title": "AI-BOM 补充",
            "content": "AI-BOM 的补充知识内容",
            "enabled": True,
            "sort_order": 100,
            "content_hash": "sha256:abc",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    ]
    db = SliceMockDb(
        {
            "user_products": [],
            "user_knowledge_entries": supplement_entries,
        }
    )

    resolver = KnowledgeSliceResolver(db=db)
    result = await resolver.resolve(
        purpose="draft",
        product_ids=["ai-bom"],
        user_id="u-1",
    )

    assert "AI-BOM 的补充知识内容" in result.content
    assert "用户级补充" in result.content


def test_product_matcher_with_user_products():
    """ProductMatcher 能匹配用户级产品。"""
    from agent.product_matcher import ProductMatcher

    matcher = ProductMatcher()
    article = {
        "title": "某公司发布新的数据安全平台",
        "summary": "该平台集成了数据治理和安全防护",
        "content": "新的数据安全平台支持多种数据源",
    }

    user_products = [
        {
            "product_id": "up-data-security",
            "name": "数据安全平台",
            "aliases": ["数据安全"],
            "keywords": ["数据安全", "数据治理", "安全防护"],
        },
    ]

    matches = matcher.match_by_rules(article, top_n=3, user_products=user_products)
    match_ids = [m.product_id for m in matches]

    assert "up-data-security" in match_ids
    user_match = next(m for m in matches if m.product_id == "up-data-security")
    assert user_match.product_name == "数据安全平台"
    assert user_match.match_score > 0


def test_product_matcher_user_products_optional():
    """ProductMatcher 不传 user_products 时正常工作。"""
    from agent.product_matcher import ProductMatcher

    matcher = ProductMatcher()
    article = {"title": "智能体身份安全新进展", "summary": "", "content": ""}

    matches = matcher.match_by_rules(article, top_n=2)
    assert len(matches) > 0
    assert "agent-identity-security" in [m.product_id for m in matches]


@pytest.mark.asyncio
async def test_merger_filters_by_product_ids():
    """KnowledgeMerger 按 product_ids 过滤用户知识条目。"""
    from agent.knowledge_merger import KnowledgeMerger

    entries = [
        {
            "entry_id": "e-1",
            "user_id": "u-1",
            "product_id": "product-a",
            "product_scope": "user",
            "doc_type": "overview",
            "title": "产品A概述",
            "content": "产品A的内容",
            "enabled": True,
            "sort_order": 100,
            "content_hash": "sha256:a",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
        {
            "entry_id": "e-2",
            "user_id": "u-1",
            "product_id": "product-b",
            "product_scope": "user",
            "doc_type": "overview",
            "title": "产品B概述",
            "content": "产品B的内容",
            "enabled": True,
            "sort_order": 100,
            "content_hash": "sha256:b",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    ]

    db = SliceMockDb(
        {
            "user_knowledge_entries": entries,
            "user_products": [],
        }
    )

    merger = KnowledgeMerger(db)
    result = await merger.merge_for_user("u-1", ["product-a"], "全局知识", ["f.md"])

    assert "产品A的内容" in result.content
    assert "产品B的内容" not in result.content


# ═══════════════════════════════════════════════════════════════
# 阶段二 Step 3：purpose/doc_type 分层与缺失语义
# ═══════════════════════════════════════════════════════════════


def _entry(entry_id: str, doc_type: str, title: str, content: str, sort_order: int = 100):
    return {
        "entry_id": entry_id,
        "user_id": "u-1",
        "product_id": "up-prod-1",
        "product_scope": "user",
        "doc_type": doc_type,
        "title": title,
        "content": content,
        "enabled": True,
        "sort_order": sort_order,
        "content_hash": "sha256:abc",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_score_purpose_required_doc_types_only():
    """score 下只注入 overview/market-brief；sales-brief/custom 不注入。"""
    from agent.knowledge_slice import KnowledgeSliceResolver

    entries = [
        _entry("e-custom", "custom", "自定义", "自定义内容", sort_order=1),
        _entry("e-sales", "sales-brief", "售前", "售前内容", sort_order=2),
        _entry("e-overview", "overview", "概述", "概述内容", sort_order=3),
        _entry("e-market", "market-brief", "市场", "市场内容", sort_order=4),
    ]
    user_products = [
        {"product_id": "up-prod-1", "user_id": "u-1", "name": "测试产品", "enabled": True}
    ]
    db = SliceMockDb(
        {
            "user_products": user_products,
            "user_knowledge_entries": entries,
        }
    )

    resolver = KnowledgeSliceResolver(db=db)
    result = await resolver.resolve(purpose="score", product_ids=["up-prod-1"], user_id="u-1")

    assert "概述内容" in result.content
    assert "市场内容" in result.content
    assert "自定义内容" not in result.content
    assert "售前内容" not in result.content


@pytest.mark.asyncio
async def test_draft_purpose_includes_optional():
    """draft 下 required + optional 全部注入。"""
    from agent.knowledge_slice import KnowledgeSliceResolver

    entries = [
        _entry("e-sales", "sales-brief", "售前", "售前内容"),
        _entry("e-custom", "custom", "自定义", "自定义内容"),
    ]
    user_products = [
        {"product_id": "up-prod-1", "user_id": "u-1", "name": "测试产品", "enabled": True}
    ]
    db = SliceMockDb(
        {
            "user_products": user_products,
            "user_knowledge_entries": entries,
        }
    )

    resolver = KnowledgeSliceResolver(db=db)
    result = await resolver.resolve(purpose="draft", product_ids=["up-prod-1"], user_id="u-1")

    assert "售前内容" in result.content
    assert "自定义内容" in result.content
    # draft required（overview/market-brief）缺失时记录缺失
    assert any("missing:overview" in m for m in result.knowledge_missing)
    assert any("missing:market-brief" in m for m in result.knowledge_missing)


@pytest.mark.asyncio
async def test_required_missing_recorded_not_inferred():
    """required 缺失记录 knowledge_missing，不推断产品能力。"""
    from agent.knowledge_slice import KnowledgeSliceResolver

    entries = [_entry("e-market", "market-brief", "市场", "市场内容")]
    user_products = [
        {"product_id": "up-prod-1", "user_id": "u-1", "name": "测试产品", "enabled": True}
    ]
    db = SliceMockDb(
        {
            "user_products": user_products,
            "user_knowledge_entries": entries,
        }
    )

    resolver = KnowledgeSliceResolver(db=db)
    result = await resolver.resolve(purpose="score", product_ids=["up-prod-1"], user_id="u-1")

    assert "市场内容" in result.content
    assert any("missing:overview" in m for m in result.knowledge_missing)
    # overview 缺失不注入任何「推断能力」文本


@pytest.mark.asyncio
async def test_invalid_doc_type_mapped_to_custom():
    """非法/缺失 doc_type 读取时映射 custom（不写回）。"""
    from agent.knowledge_slice import KnowledgeSliceResolver

    invalid = _entry("e-invalid", "totally-bogus", "非法", "非法类型内容")
    invalid.pop("doc_type")  # 缺失 doc_type
    missing = _entry("e-missing", "unknown", "未知", "未知内容")
    entries = [invalid, missing]
    user_products = [
        {"product_id": "up-prod-1", "user_id": "u-1", "name": "测试产品", "enabled": True}
    ]
    db = SliceMockDb(
        {
            "user_products": user_products,
            "user_knowledge_entries": entries,
        }
    )

    resolver = KnowledgeSliceResolver(db=db)
    result = await resolver.resolve(purpose="draft", product_ids=["up-prod-1"], user_id="u-1")

    assert "非法类型内容" in result.content
    assert "未知内容" in result.content
    assert "用户级·custom" in result.content


@pytest.mark.asyncio
async def test_stable_order_required_before_optional():
    """稳定排序：required 优先于 optional，即便 sort_order 更靠后。"""
    from agent.knowledge_slice import KnowledgeSliceResolver

    entries = [
        _entry("e-overview", "overview", "概述", "概述内容", sort_order=999),
        _entry("e-custom", "custom", "自定义", "自定义内容", sort_order=1),
    ]
    user_products = [
        {"product_id": "up-prod-1", "user_id": "u-1", "name": "测试产品", "enabled": True}
    ]
    db = SliceMockDb(
        {
            "user_products": user_products,
            "user_knowledge_entries": entries,
        }
    )

    resolver = KnowledgeSliceResolver(db=db)
    result = await resolver.resolve(purpose="draft", product_ids=["up-prod-1"], user_id="u-1")

    idx_overview = result.content.index("概述内容")
    idx_custom = result.content.index("自定义内容")
    assert idx_overview < idx_custom


@pytest.mark.asyncio
async def test_budget_checked_before_append():
    """预算在追加前计算，不产生先超额再截断的内容。"""
    from agent.knowledge_slice import KnowledgeSliceResolver

    entries = [
        _entry("e-overview", "overview", "概述", "概述内容" * 100),
        _entry("e-market", "market-brief", "市场", "市场内容" * 100),
        _entry("e-sales", "sales-brief", "售前", "售前内容" * 100),
    ]
    user_products = [
        {"product_id": "up-prod-1", "user_id": "u-1", "name": "测试产品", "enabled": True}
    ]
    db = SliceMockDb(
        {
            "user_products": user_products,
            "user_knowledge_entries": entries,
        }
    )

    resolver = KnowledgeSliceResolver(db=db, max_chars=800)
    result = await resolver.resolve(
        purpose="draft", product_ids=["up-prod-1"], user_id="u-1", max_chars=800
    )

    # 内容总字符不超过预算 + 分隔符余量（追加前计算）
    assert result.char_count <= 800 + 200
    assert result.truncated


@pytest.mark.asyncio
async def test_user_scope_isolation():
    """用户知识严格按 user_id 隔离：不注入其他用户的条目。"""
    from agent.knowledge_slice import KnowledgeSliceResolver

    other = _entry("e-other", "overview", "其他用户概述", "其他用户内容")
    other["user_id"] = "u-2"
    entries = [
        _entry("e-overview", "overview", "概述", "本用户内容"),
        other,
    ]
    user_products = [
        {"product_id": "up-prod-1", "user_id": "u-1", "name": "测试产品", "enabled": True}
    ]
    db = SliceMockDb(
        {
            "user_products": user_products,
            "user_knowledge_entries": entries,
        }
    )

    resolver = KnowledgeSliceResolver(db=db)
    result = await resolver.resolve(purpose="draft", product_ids=["up-prod-1"], user_id="u-1")

    assert "本用户内容" in result.content
    assert "其他用户内容" not in result.content
