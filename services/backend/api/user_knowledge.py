"""阶段十五 T1：用户知识条目与用户产品注册 API。"""

from __future__ import annotations

import logging

from agent.product_catalog import ProductCatalogService
from auth.deps import AuthError, get_current_user
from config import get_settings
from fastapi import APIRouter, Depends, Request
from models.user_knowledge import (
    ProductCatalogItem,
    ProductCatalogList,
    ProductScope,
    UserKnowledgeEntry,
    UserKnowledgeEntryCreate,
    UserKnowledgeEntryList,
    UserKnowledgeEntryUpdate,
    UserProduct,
    UserProductCreate,
    UserProductUpdate,
    compute_content_hash,
    utc_now,
)

logger = logging.getLogger("backend.api.user_knowledge")

router = APIRouter(prefix="/api/user-knowledge", tags=["User Knowledge"])


class UserKnowledgeError(AuthError):
    """用户知识库统一 API 错误。"""


def _get_db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise UserKnowledgeError(503, "DATABASE_UNAVAILABLE", "数据库暂不可用")
    return db


def _without_mongo_id(document: dict) -> dict:
    result = dict(document)
    result.pop("_id", None)
    return result


def _entry_from_doc(document: dict) -> UserKnowledgeEntry:
    return UserKnowledgeEntry(**_without_mongo_id(document))


def _product_from_doc(document: dict) -> UserProduct:
    return UserProduct(**_without_mongo_id(document))


def _catalog() -> ProductCatalogService:
    return ProductCatalogService(get_settings().KNOWLEDGE_BASE_DIR)


async def _validate_product_reference(
    db,
    *,
    user_id: str,
    product_id: str,
    product_scope: ProductScope,
) -> None:
    """确保知识条目引用的是已发布全局产品或当前用户的启用产品。"""
    if product_scope == ProductScope.GLOBAL:
        try:
            _catalog().validate_product_id(product_id)
        except ValueError as exc:
            raise UserKnowledgeError(422, "PRODUCT_UNAVAILABLE", str(exc)) from None
        return

    product = await db["user_products"].find_one(
        {"user_id": user_id, "product_id": product_id, "enabled": True}
    )
    if product is None:
        raise UserKnowledgeError(422, "PRODUCT_UNAVAILABLE", "用户产品不存在或已禁用")


async def _find_owned_entry(db, user_id: str, entry_id: str) -> dict:
    document = await db["user_knowledge_entries"].find_one(
        {"entry_id": entry_id, "user_id": user_id}
    )
    if document is None:
        raise UserKnowledgeError(404, "KNOWLEDGE_ENTRY_NOT_FOUND", "知识条目不存在")
    return document


async def _find_owned_product(db, user_id: str, product_id: str) -> dict:
    document = await db["user_products"].find_one({"product_id": product_id, "user_id": user_id})
    if document is None:
        raise UserKnowledgeError(404, "USER_PRODUCT_NOT_FOUND", "用户产品不存在")
    return document


# 产品路由必须先于 /{entry_id} 注册，避免静态路径被动态路径吞掉。
@router.get("/products", summary="列出当前用户可见的全局产品和用户产品")
async def list_products(
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    items = [
        ProductCatalogItem(
            product_id=product.product_id,
            name=product.name,
            description=product.description,
            scope=ProductScope.GLOBAL,
            aliases=list(product.aliases),
            sort_order=product.sort_order,
            enabled=True,
            available_for=list(product.allowed_purposes),
        )
        for product in _catalog().list_products(published_only=True)
    ]

    documents = (
        await db["user_products"]
        .find({"user_id": user_id})
        .sort("sort_order", 1)
        .to_list(length=500)
    )
    items.extend(
        ProductCatalogItem(
            product_id=product.product_id,
            name=product.name,
            description=product.description,
            scope=ProductScope.USER,
            aliases=product.aliases,
            keywords=product.keywords,
            sort_order=product.sort_order,
            enabled=product.enabled,
            available_for=["score", "draft", "chat"] if product.enabled else [],
        )
        for product in (_product_from_doc(document) for document in documents)
    )
    data = ProductCatalogList(items=items, total=len(items))
    return {"ok": True, "data": data.model_dump(mode="json")}


@router.post("/products", summary="注册用户级产品")
async def create_product(
    body: UserProductCreate,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    duplicate = await db["user_products"].find_one({"user_id": user_id, "name": body.name})
    if duplicate is not None:
        raise UserKnowledgeError(409, "USER_PRODUCT_NAME_EXISTS", "同名用户产品已存在")

    now = utc_now()
    product = UserProduct(
        user_id=user_id,
        **body.model_dump(),
        created_at=now,
        updated_at=now,
    )
    await db["user_products"].insert_one(product.model_dump())
    logger.info("User product created: user=%s product=%s", user_id, product.product_id)
    return {"ok": True, "data": product.model_dump(mode="json")}


@router.put("/products/{product_id}", summary="更新用户级产品")
async def update_product(
    product_id: str,
    body: UserProductUpdate,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    await _find_owned_product(db, user_id, product_id)
    update_fields = body.model_dump(exclude_unset=True, exclude_none=True)
    if not update_fields:
        raise UserKnowledgeError(422, "EMPTY_UPDATE", "至少提供一个待更新字段")

    if "name" in update_fields:
        duplicate = await db["user_products"].find_one(
            {"user_id": user_id, "name": update_fields["name"], "product_id": {"$ne": product_id}}
        )
        if duplicate is not None:
            raise UserKnowledgeError(409, "USER_PRODUCT_NAME_EXISTS", "同名用户产品已存在")

    update_fields["updated_at"] = utc_now()
    await db["user_products"].update_one(
        {"product_id": product_id, "user_id": user_id},
        {"$set": update_fields},
    )
    updated = await _find_owned_product(db, user_id, product_id)
    return {"ok": True, "data": _product_from_doc(updated).model_dump(mode="json")}


@router.delete("/products/{product_id}", summary="删除用户级产品")
async def delete_product(
    product_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    await _find_owned_product(db, user_id, product_id)
    linked_entry = await db["user_knowledge_entries"].find_one(
        {
            "user_id": user_id,
            "product_id": product_id,
            "product_scope": ProductScope.USER.value,
        }
    )
    if linked_entry is not None:
        raise UserKnowledgeError(
            409,
            "USER_PRODUCT_IN_USE",
            "该产品仍有关联知识条目，请先处理关联条目",
        )

    await db["user_products"].delete_one({"product_id": product_id, "user_id": user_id})
    logger.info("User product deleted: user=%s product=%s", user_id, product_id)
    return {"ok": True, "data": {"product_id": product_id}}


@router.get("/products/{product_id}", summary="按产品筛选当前用户的知识条目")
async def list_entries_by_product(
    product_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    documents = (
        await db["user_knowledge_entries"]
        .find({"user_id": user_id, "product_id": product_id})
        .sort("sort_order", 1)
        .to_list(length=1_000)
    )
    items = [_entry_from_doc(document) for document in documents]
    data = UserKnowledgeEntryList(items=items, total=len(items))
    return {"ok": True, "data": data.model_dump(mode="json")}


@router.get("", summary="列出当前用户的知识条目")
async def list_entries(
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    documents = (
        await db["user_knowledge_entries"]
        .find({"user_id": user_id})
        .sort("sort_order", 1)
        .to_list(length=1_000)
    )
    items = [_entry_from_doc(document) for document in documents]
    data = UserKnowledgeEntryList(items=items, total=len(items))
    return {"ok": True, "data": data.model_dump(mode="json")}


@router.post("", summary="创建用户知识条目")
async def create_entry(
    body: UserKnowledgeEntryCreate,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    await _validate_product_reference(
        db,
        user_id=user_id,
        product_id=body.product_id,
        product_scope=body.product_scope,
    )
    now = utc_now()
    entry = UserKnowledgeEntry(
        user_id=user_id,
        **body.model_dump(),
        content_hash=compute_content_hash(body.content),
        created_at=now,
        updated_at=now,
    )
    await db["user_knowledge_entries"].insert_one(entry.model_dump())
    logger.info("Knowledge entry created: user=%s entry=%s", user_id, entry.entry_id)
    return {"ok": True, "data": entry.model_dump(mode="json")}


@router.get("/{entry_id}", summary="获取当前用户的单个知识条目")
async def get_entry(
    entry_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    document = await _find_owned_entry(_get_db(request), user_id, entry_id)
    return {"ok": True, "data": _entry_from_doc(document).model_dump(mode="json")}


@router.put("/{entry_id}", summary="更新当前用户的知识条目")
async def update_entry(
    entry_id: str,
    body: UserKnowledgeEntryUpdate,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    existing = await _find_owned_entry(db, user_id, entry_id)
    update_fields = body.model_dump(mode="json", exclude_unset=True, exclude_none=True)
    if not update_fields:
        raise UserKnowledgeError(422, "EMPTY_UPDATE", "至少提供一个待更新字段")

    if "product_id" in update_fields or "product_scope" in update_fields:
        await _validate_product_reference(
            db,
            user_id=user_id,
            product_id=update_fields.get("product_id", existing["product_id"]),
            product_scope=ProductScope(
                update_fields.get("product_scope", existing["product_scope"])
            ),
        )
    if "content" in update_fields:
        update_fields["content_hash"] = compute_content_hash(update_fields["content"])
    update_fields["updated_at"] = utc_now()
    await db["user_knowledge_entries"].update_one(
        {"entry_id": entry_id, "user_id": user_id},
        {"$set": update_fields},
    )
    updated = await _find_owned_entry(db, user_id, entry_id)
    return {"ok": True, "data": _entry_from_doc(updated).model_dump(mode="json")}


@router.delete("/{entry_id}", summary="删除当前用户的知识条目")
async def delete_entry(
    entry_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    await _find_owned_entry(db, user_id, entry_id)
    await db["user_knowledge_entries"].delete_one({"entry_id": entry_id, "user_id": user_id})
    logger.info("Knowledge entry deleted: user=%s entry=%s", user_id, entry_id)
    return {"ok": True, "data": {"entry_id": entry_id}}


@router.post("/{entry_id}/toggle", summary="切换当前用户知识条目的启用状态")
async def toggle_entry(
    entry_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    existing = await _find_owned_entry(db, user_id, entry_id)
    enabled = not bool(existing.get("enabled", True))
    await db["user_knowledge_entries"].update_one(
        {"entry_id": entry_id, "user_id": user_id},
        {"$set": {"enabled": enabled, "updated_at": utc_now()}},
    )
    return {"ok": True, "data": {"entry_id": entry_id, "enabled": enabled}}
