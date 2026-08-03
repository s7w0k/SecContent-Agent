"""产品目录 API - 列出已发布产品。"""

from __future__ import annotations

from typing import Literal

from agent.product_catalog import ProductCatalogService
from auth.deps import get_current_user
from config import get_settings
from fastapi import APIRouter, Depends, Query

router = APIRouter(prefix="/api/product-catalog", tags=["Product Catalog"])


@router.get("", summary="获取产品目录")
async def get_product_catalog(
    purpose: Literal["score", "draft", "chat"] | None = Query(None),
    _user_id: str = Depends(get_current_user),
):
    """列出已发布产品。

    Args:
        purpose: 筛选用途，None 返回所有已发布产品
    """
    settings = get_settings()
    catalog = ProductCatalogService(settings.KNOWLEDGE_BASE_DIR)
    return {"ok": True, "data": catalog.to_api_response(purpose=purpose)}
