"""RunManifest — 统一运行清单（阶段3 WBS 3.1；统一架构文档 §2）。

每次运行开始时冻结不可变运行清单：执行模式、代码版本、模型与价格表版本、
提示词/技能/知识/上下文版本指纹、工具契约版本、特性开关、预算与验收条件。

设计约束：
  - manifest 冻结后不可变（frozen=True），run 过程中不得改写；
  - validate_manifest 在 run 启动前强制校验，清单创建失败不得启动 Agent；
  - 不保存密钥、完整正文或私有思维链（只存指纹与版本引用）；
  - run_id 关联 RuntimeState（运行中可变状态）与 RunManifest（启动前冻结契约），
    追溯页合并两者回答"为什么这样执行"。

与阶段2 数据集指纹 / 阶段1 RunContext 的关系：
  - RunContext.deadline_at / allowed_* 是执行期权限边界；
  - RunManifest 冻结的是"这一 run 用什么代码/模型/知识版本跑的"。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from agent.runtime_state import RunBudget
from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

logger = logging.getLogger("backend.agent.run_manifest")

SCHEMA_VERSION = "1.0"
COLLECTION = "runtime_manifests"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ManifestError(ValueError):
    """清单非法（缺失必填字段 / 预算未冻结）。"""


class ExecutionMode(StrEnum):
    """统一执行模式（与目标架构链路对齐）。"""

    LEGACY = "legacy"
    AGENTLOOP = "agentloop"
    PLANNED = "planned"
    AUTONOMOUS = "autonomous"
    SHADOW = "shadow"


class RunManifest(BaseModel):
    """启动前冻结的运行清单（不可变）。"""

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    run_id: str = Field(..., min_length=1, max_length=100)
    thread_id: str = ""
    trace_id: str = ""
    user_id: str = Field(..., min_length=1, max_length=100)
    tenant_id: str = ""

    execution_mode: ExecutionMode = ExecutionMode.AUTONOMOUS
    code_revision: str = ""  # Git commit 或镜像 digest
    model_provider: str = ""
    model_id: str = ""
    model_revision: str = ""
    pricing_version: str = ""  # 成本价格表版本

    prompt_refs: list[dict[str, str]] = Field(default_factory=list)  # [{key, version, hash}]
    skill_snapshot_hash: str = ""
    knowledge_snapshot_hash: str = ""
    context_plan_hash: str = ""
    tool_registry_version: str = ""

    feature_flags: dict[str, Any] = Field(default_factory=dict)  # 实际生效的开关与灰度桶
    budget: RunBudget = Field(default_factory=RunBudget)
    acceptance_criteria: list[str] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=_utc_now)


def build_run_manifest(
    *,
    run_id: str,
    user_id: str,
    tenant_id: str = "",
    thread_id: str = "",
    trace_id: str = "",
    execution_mode: ExecutionMode | str = ExecutionMode.AUTONOMOUS,
    code_revision: str = "",
    model_provider: str = "",
    model_id: str = "",
    model_revision: str = "",
    pricing_version: str = "",
    prompt_refs: list[dict[str, str]] | None = None,
    skill_snapshot_hash: str = "",
    knowledge_snapshot_hash: str = "",
    context_plan_hash: str = "",
    tool_registry_version: str = "",
    feature_flags: dict[str, Any] | None = None,
    budget: RunBudget | None = None,
    acceptance_criteria: list[str] | None = None,
    created_at: datetime | None = None,
) -> RunManifest:
    """构造并冻结清单（执行模式统一字符串 → StrEnum）。"""
    return RunManifest(
        run_id=run_id,
        user_id=user_id,
        tenant_id=tenant_id,
        thread_id=thread_id,
        trace_id=trace_id,
        execution_mode=ExecutionMode(execution_mode),
        code_revision=code_revision,
        model_provider=model_provider,
        model_id=model_id,
        model_revision=model_revision,
        pricing_version=pricing_version,
        prompt_refs=list(prompt_refs or []),
        skill_snapshot_hash=skill_snapshot_hash,
        knowledge_snapshot_hash=knowledge_snapshot_hash,
        context_plan_hash=context_plan_hash,
        tool_registry_version=tool_registry_version,
        feature_flags=dict(feature_flags or {}),
        budget=budget or RunBudget(),
        acceptance_criteria=list(acceptance_criteria or []),
        created_at=created_at or _utc_now(),
    )


def manifest_fingerprint(manifest: RunManifest) -> str:
    """清单指纹：全部可复现字段的稳定哈希（run 复现与候选对比锚点）。"""
    raw = json.dumps(
        manifest.model_dump(mode="json", exclude={"created_at"}),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_manifest(manifest: RunManifest) -> None:
    """启动前校验：清单缺失关键信息 / 预算未冻结则拒绝启动 Agent。"""
    if not manifest.run_id:
        raise ManifestError("run_id 缺失，无法冻结清单")
    if not manifest.user_id:
        raise ManifestError("user_id 缺失，无法冻结清单")
    if not manifest.code_revision:
        raise ManifestError("code_revision 缺失：必须记录 Git commit 或镜像 digest")
    if not manifest.tool_registry_version:
        raise ManifestError("tool_registry_version 缺失：工具契约版本必须冻结")
    if manifest.budget.max_steps <= 0:
        raise ManifestError("预算未冻结：max_steps 必须 > 0")


# ═══════════════════════════════════════════════════════════════
# 持久化
# ═══════════════════════════════════════════════════════════════


class RunManifestStore:
    """runtime_manifests 集合：清单按 run_id 唯一持久化。"""

    def __init__(self, db: Any, *, collection: str = COLLECTION):
        self.db = db
        self.collection_name = collection
        self.col = db[collection]

    def index_specs(self) -> dict[str, list[IndexModel]]:
        return {
            self.collection_name: [
                IndexModel([("run_id", ASCENDING)], unique=True, name="uq_manifest_run_id"),
                IndexModel(
                    [("user_id", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_manifest_user_created",
                ),
                IndexModel(
                    [("code_revision", ASCENDING)], name="idx_manifest_code_revision"
                ),
            ]
        }

    async def ensure_indexes(self) -> list[str]:
        return await self.col.create_indexes(self.index_specs()[self.collection_name])

    async def save(self, manifest: RunManifest) -> None:
        """保存清单（upsert；清单不可变，重复保存幂等）。"""
        doc = manifest.model_dump(mode="json")
        await self.col.replace_one({"run_id": manifest.run_id}, doc, upsert=True)

    async def load(self, run_id: str) -> RunManifest | None:
        doc = await self.col.find_one({"run_id": run_id})
        if doc is None:
            return None
        return RunManifest.model_validate(doc)

    async def list_manifests(
        self, *, user_id: str = "", code_revision: str = "", limit: int = 50
    ) -> list[RunManifest]:
        query: dict[str, Any] = {}
        if user_id:
            query["user_id"] = user_id
        if code_revision:
            query["code_revision"] = code_revision
        try:
            cursor = self.col.find(query).sort("created_at", -1).limit(limit)
            docs = await cursor.to_list(length=limit)
            return [RunManifest.model_validate(d) for d in docs]
        except Exception:
            logger.warning("[run_manifest] list failed")
            return []
