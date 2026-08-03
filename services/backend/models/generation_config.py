"""用户生成偏好和流水线配置快照模型。

包含：
- ProductTargetMode / ScoreMode 枚举
- GenerationOptions（单次请求覆盖）
- UserGenerationPreferences（账号级默认偏好）
- PipelineConfigSnapshot（任务冻结快照）
- PromptRef（提示词版本引用）
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ProductTargetMode(StrEnum):
    """产品选择模式。"""

    NONE = "none"
    AUTO = "auto"
    SELECTED = "selected"


class ScoreMode(StrEnum):
    """评分模式。"""

    PRODUCT_EVENT = "product_event"
    EVENT_ONLY = "event_only"


class GenerationOptions(BaseModel):
    """单次生成请求的覆盖选项。

    None 表示使用账号级默认偏好。
    """

    product_relevance_enabled: bool | None = None
    product_target_mode: ProductTargetMode | None = None
    selected_product_ids: list[str] = Field(default_factory=list, max_length=5)
    force_generate: bool = False

    @model_validator(mode="after")
    def validate_mode_consistency(self) -> GenerationOptions:
        """none 模式自动关闭产品相关性。"""
        if self.product_target_mode == ProductTargetMode.NONE:
            if self.product_relevance_enabled is None:
                self.product_relevance_enabled = False
            elif self.product_relevance_enabled is True:
                raise ValueError("none 模式不允许启用产品相关性")
        if self.product_target_mode == ProductTargetMode.SELECTED and self.selected_product_ids is not None and len(self.selected_product_ids) == 0:
            raise ValueError("selected 模式必须指定至少一个产品")
        return self


class PromptRef(BaseModel):
    """任务快照中引用的提示词版本。"""

    prompt_key: str
    source: str  # "system" | "user"
    version: int
    content_hash: str


class PipelineConfigSnapshot(BaseModel):
    """流水线任务冻结的配置快照。"""

    schema_version: int = 1
    product_relevance_enabled: bool = True
    score_mode: ScoreMode = ScoreMode.PRODUCT_EVENT
    product_target_mode: ProductTargetMode = ProductTargetMode.AUTO
    selected_product_ids: list[str] = Field(default_factory=list)
    resolved_products: list[dict[str, Any]] = Field(default_factory=list)
    prompt_refs: list[PromptRef] = Field(default_factory=list)
    knowledge_hash: str = ""
    config_fingerprint: str = ""
    force_generate: bool = False


class UserGenerationPreferences(BaseModel):
    """账号级默认生成偏好。"""

    user_id: str
    product_relevance_enabled: bool = True
    product_target_mode: ProductTargetMode = ProductTargetMode.AUTO
    selected_product_ids: list[str] = Field(default_factory=list, max_length=5)
    product_event_threshold: int = 80
    event_only_threshold: int = 60
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def validate_consistency(self) -> UserGenerationPreferences:
        if self.product_target_mode == ProductTargetMode.NONE and self.product_relevance_enabled:
            raise ValueError("none 模式不允许启用产品相关性")
        if self.product_target_mode == ProductTargetMode.SELECTED and not self.selected_product_ids:
            raise ValueError("selected 模式必须指定至少一个产品")
        return self


class UserGenerationPreferencesUpdate(BaseModel):
    """保存生成偏好的请求体。"""

    product_relevance_enabled: bool = True
    product_target_mode: ProductTargetMode = ProductTargetMode.AUTO
    selected_product_ids: list[str] = Field(default_factory=list, max_length=5)
    expected_version: int | None = None

    @model_validator(mode="after")
    def validate_consistency(self) -> UserGenerationPreferencesUpdate:
        if self.product_target_mode == ProductTargetMode.NONE and self.product_relevance_enabled:
            raise ValueError("none 模式不允许启用产品相关性")
        if self.product_target_mode == ProductTargetMode.SELECTED and not self.selected_product_ids:
            raise ValueError("selected 模式必须指定至少一个产品")
        return self


def compute_config_fingerprint(
    *,
    product_relevance_enabled: bool,
    product_target_mode: str,
    selected_product_ids: list[str],
    knowledge_hash: str,
    prompt_refs: list[dict],
) -> str:
    """计算配置指纹（SHA256）。"""
    import hashlib
    import json

    parts = {
        "product_relevance_enabled": product_relevance_enabled,
        "product_target_mode": product_target_mode,
        "selected_product_ids": sorted(selected_product_ids),
        "knowledge_hash": knowledge_hash,
        "prompt_refs": sorted(prompt_refs, key=lambda x: x.get("prompt_key", "")),
    }
    content = json.dumps(parts, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def merge_options_with_preferences(
    options: GenerationOptions | None,
    preferences: UserGenerationPreferences | None,
) -> tuple[bool, ProductTargetMode, list[str]]:
    """合并单次请求选项与账号级默认偏好。

    单次请求优先 > 账号级偏好 > 系统默认。
    """
    # 系统默认
    sys_relevance = True
    sys_mode = ProductTargetMode.AUTO
    sys_products: list[str] = []

    if preferences is not None:
        sys_relevance = preferences.product_relevance_enabled
        sys_mode = preferences.product_target_mode
        sys_products = list(preferences.selected_product_ids)

    if options is None:
        return sys_relevance, sys_mode, sys_products

    relevance = options.product_relevance_enabled if options.product_relevance_enabled is not None else sys_relevance
    mode = options.product_target_mode if options.product_target_mode is not None else sys_mode
    products = list(options.selected_product_ids) if options.selected_product_ids else sys_products

    # none 强制关闭
    if mode == ProductTargetMode.NONE:
        relevance = False

    return relevance, mode, products
