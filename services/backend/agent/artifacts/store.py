"""ArtifactStore - 产物持久化（计划 §18 / §46）。

按 artifact_type + parent 维护版本，内容哈希为 canonical JSON 的 sha256。
默认内存实现便于测试；生产可注入 Mongo-backed store（接口一致）。
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from agent.artifacts.contracts import ArtifactRef, ArtifactType

MAX_CONTENT_HASH_LEN = 64


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")
    )


def content_hash_of(payload: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class ArtifactStore:
    """版本化产物库。

    put() 对同一 (artifact_type, parent_ref) 递增 version 并写记录；
    get() 返回 payload；ref() 生成 ArtifactRef。
    """

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._payloads: dict[str, dict[str, Any]] = {}
        self._next_version: dict[tuple[str, str], int] = defaultdict(int)

    def _bump_version(self, artifact_type: str, parent_ref: str) -> int:
        self._next_version[(artifact_type, parent_ref)] += 1
        return self._next_version[(artifact_type, parent_ref)]

    async def put(
        self,
        *,
        artifact_type: str,
        payload: dict[str, Any],
        producer: str,
        run_id: str,
        step_id: str = "",
        parent_ref: str | None = None,
        shadow: bool = False,
        tenant_id: str = "",
        user_id: str = "",
    ) -> dict[str, Any]:
        """写入并返回 artifact 记录（计划 §46 ArtifactRef 字段 + ref）。"""
        artifact_id = payload.get("artifact_id") or f"art-{len(self._records) + 1}"
        parent_key = parent_ref or ""
        version = self._bump_version(artifact_type, parent_key)
        art_type: ArtifactType = artifact_type  # type: ignore[assignment]
        content_hash = content_hash_of(payload)
        record = ArtifactRef(
            artifact_id=artifact_id,
            artifact_type=art_type,
            version=version,
            content_hash=content_hash,
            producer=producer,
            run_id=run_id,
            step_id=step_id,
            created_at=datetime.now(UTC),
        ).model_dump(mode="json")
        record["parent_ref"] = parent_ref
        record["shadow"] = bool(shadow)
        record["tenant_id"] = tenant_id
        record["user_id"] = user_id
        record["ref"] = ArtifactRef(**{k: v for k, v in record.items() if k != "parent_ref"}).ref
        key = f"{artifact_type}:{artifact_id}@{version}"
        self._records[key] = record
        self._payloads[key] = dict(payload)
        return record

    async def get(self, *, artifact_id: str, artifact_type: str, version: int) -> dict[str, Any]:
        key = f"{artifact_type}:{artifact_id}@{version}"
        if key not in self._payloads:
            raise KeyError(f"artifact not found: {key}")
        return dict(self._payloads[key])

    async def get_record(
        self, *, artifact_id: str, artifact_type: str, version: int
    ) -> dict[str, Any]:
        key = f"{artifact_type}:{artifact_id}@{version}"
        if key not in self._records:
            raise KeyError(f"artifact record not found: {key}")
        return dict(self._records[key])

    def __len__(self) -> int:
        return len(self._payloads)
