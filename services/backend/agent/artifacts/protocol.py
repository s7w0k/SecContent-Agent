"""ArtifactStore Protocol（OneShot Cutover 计划 §19 / §97）。

Tests → 内存 ArtifactStore；生产 → MongoArtifactStore。两者都满足本 Protocol。
统一三个异步方法：put / get / get_record。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ArtifactStoreProtocol(Protocol):
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
    ) -> dict[str, Any]: ...

    async def get(
        self, *, artifact_id: str, artifact_type: str, version: int
    ) -> dict[str, Any]: ...

    async def get_record(
        self, *, artifact_id: str, artifact_type: str, version: int
    ) -> dict[str, Any]: ...


__all__ = ["ArtifactStoreProtocol"]
