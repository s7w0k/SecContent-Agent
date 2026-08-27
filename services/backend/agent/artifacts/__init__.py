"""Artifact Layer - 模块导出。"""

from __future__ import annotations

from agent.artifacts.contracts import AgentHandoff, ArtifactRef, ArtifactType
from agent.artifacts.mongo_store import ARTIFACT_INDEXES, MongoArtifactStore
from agent.artifacts.protocol import ArtifactStoreProtocol
from agent.artifacts.store import ArtifactStore, content_hash_of

__all__ = [
    "ARTIFACT_INDEXES",
    "AgentHandoff",
    "ArtifactRef",
    "ArtifactStore",
    "ArtifactStoreProtocol",
    "ArtifactType",
    "MongoArtifactStore",
    "content_hash_of",
]
