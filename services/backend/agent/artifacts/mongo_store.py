"""MongoArtifactStore - 生产持久化 ArtifactStore（OneShot Cutover 计划 §16 / §17 / §18）。

接口与内存 ArtifactStore 一致（§16 / §19 ArtifactStoreProtocol），但持久化到 Mongo：
  - collection: ``agent_artifacts``
  - 唯一索引：``artifact_type + artifact_id + version``
  - 索引：run_id / tenant_id+artifact_id / parent_ref / created_at

Schema 字段（§17）：artifact_id / artifact_type / version / content_hash / producer /
run_id / step_id / parent_ref / payload / tenant_id / user_id / shadow / created_at。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent.artifacts.contracts import ArtifactRef, ArtifactType
from agent.artifacts.store import content_hash_of
from pymongo import ASCENDING, DESCENDING, IndexModel

# §18：唯一约束 + 查询索引
ARTIFACT_INDEXES: list[IndexModel] = [
    IndexModel(
        [("artifact_type", ASCENDING), ("artifact_id", ASCENDING), ("version", ASCENDING)],
        unique=True,
        name="uq_artifact_type_id_version",
    ),
    IndexModel([("run_id", ASCENDING)], name="idx_artifact_run_id"),
    IndexModel(
        [("tenant_id", ASCENDING), ("artifact_id", ASCENDING)],
        name="idx_artifact_tenant_id",
    ),
    IndexModel([("parent_ref", ASCENDING)], name="idx_artifact_parent_ref"),
    IndexModel([("created_at", DESCENDING)], name="idx_artifact_created_at"),
]


class MongoArtifactStore:
    """Mongo 持久化产物库。构造不建索引；建议启动时调用 ensure_indexes()。"""

    COLLECTION = "agent_artifacts"

    def __init__(self, db: Any) -> None:
        self._db = db

    @property
    def collection(self) -> Any:
        return self._db[self.COLLECTION]

    async def ensure_indexes(self) -> list[str]:
        return await self.collection.create_indexes(ARTIFACT_INDEXES)

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
        version = await self._next_version(artifact_type, payload.get("artifact_id") or "", run_id)
        artifact_id = payload.get("artifact_id") or f"art-{run_id}-{version}"
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
        record["payload"] = payload
        record["ref"] = ArtifactRef(**{k: v for k, v in record.items() if k != "parent_ref"}).ref
        await self.collection.insert_one(record)
        return record

    async def get(self, *, artifact_id: str, artifact_type: str, version: int) -> dict[str, Any]:
        doc = await self.collection.find_one(
            {"artifact_type": artifact_type, "artifact_id": artifact_id, "version": version}
        )
        if doc is None:
            raise KeyError(f"artifact not found: {artifact_type}:{artifact_id}@{version}")
        return dict(doc.get("payload") or {})

    async def get_record(
        self, *, artifact_id: str, artifact_type: str, version: int
    ) -> dict[str, Any]:
        doc = await self.collection.find_one(
            {"artifact_type": artifact_type, "artifact_id": artifact_id, "version": version}
        )
        if doc is None:
            raise KeyError(f"artifact record not found: {artifact_type}:{artifact_id}@{version}")
        recurded = {k: v for k, v in doc.items() if k not in ("_id", "payload")}
        recurded["_id"] = str(doc.get("_id"))
        return recurded

    async def _next_version(self, artifact_type: str, artifact_id: str, run_id: str) -> int:
        if not artifact_id:
            # 无显式 artifact_id：以 run 内计数近似，再从库内找最大单件 version + 1
            doc = await self.collection.find_one(
                {"artifact_type": artifact_type, "run_id": run_id},
                sort=[("version", DESCENDING)],
            )
            return int((doc or {}).get("version", 0) or 0) + 1
        doc = await self.collection.find_one(
            {"artifact_type": artifact_type, "artifact_id": artifact_id},
            sort=[("version", DESCENDING)],
        )
        return int((doc or {}).get("version", 0) or 0) + 1


__all__ = ["ARTIFACT_INDEXES", "MongoArtifactStore"]
