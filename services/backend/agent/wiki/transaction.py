"""KnowledgeBuildTransaction - Wiki 构建事务（Phase1 / G-01 修复）。

目标：保证 "scan → compile → lint → gate → publish → commit" 是事务式的。
- Source Registry 的 active 快照只在 Publish 成功后才被 commit；
- Compile/Lint/Publish 任一步失败都不会污染 active 快照；
- 同一 build_id 重放保持幂等；
- Maintainer 启动时对未完成事务做 Crash Recovery（见 RECOVERY_ACTIONS）。

设计约束：
- SourceTag 不在此处，事务只负责编排与持久化状态；
- 所有落盘为原子写（tmp + replace），损坏事务跳过并告警。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger("backend.agent.wiki.transaction")

BuildState = Literal[
    "DISCOVERED",
    "COMPILING",
    "COMPILED",
    "VALIDATING",
    "READY_TO_PUBLISH",
    "PUBLISHING",
    "PUBLISHED",
    "COMMITTED",
    "FAILED",
    "ROLLED_BACK",
]

# 允许的状态迁移（含 Crash Recovery 重试路径）
_ALLOWED_TRANSITIONS: dict[BuildState, set[str]] = {
    "DISCOVERED": {"COMPILING", "FAILED"},
    "COMPILING": {"COMPILED", "VALIDATING", "PUBLISHING", "FAILED"},
    "COMPILED": {"VALIDATING", "READY_TO_PUBLISH", "FAILED"},
    "VALIDATING": {"READY_TO_PUBLISH", "FAILED"},
    "READY_TO_PUBLISH": {"PUBLISHING", "FAILED"},
    "PUBLISHING": {"PUBLISHED", "FAILED"},
    "PUBLISHED": {"COMMITTED", "FAILED", "ROLLED_BACK"},
    "FAILED": {"DISCOVERED", "COMPILING"},  # 有界重试
    "COMMITTED": set(),
    "ROLLED_BACK": set(),
}

# 未完成事务的 Crash Recovery 动作（按状态）
RECOVERY_ACTIONS: dict[BuildState, str] = {
    "DISCOVERED": "RESTART_COMPILE",
    "COMPILING": "CLEANUP_STAGING_AND_COMPILE",
    "COMPILED": "RERUN_LINT",
    "VALIDATING": "RERUN_LINT",
    "READY_TO_PUBLISH": "PUBLISH_AFTER_STAGING_HASH_CHECK",
    "PUBLISHING": "CHECK_ACTIVE_POINTER",
    "PUBLISHED": "COMMIT_REGISTRY",
    "COMMITTED": "NOOP",
    "FAILED": "MANUAL_REVIEW",
    "ROLLED_BACK": "NOOP",
}

TERMINAL_STATES: frozenset[str] = frozenset({"COMMITTED", "FAILED", "ROLLED_BACK"})


def compute_build_id(
    *,
    parent_wiki_version: str,
    source_snapshot_hash: str,
    compiler_version: str,
    schema_version: int,
) -> str:
    """幂等 Build ID：同一输入重试必须得到同一 ID，不会逻辑上重复发布（G-01）。"""
    blob = "|".join(
        [parent_wiki_version, source_snapshot_hash, compiler_version, str(schema_version)]
    )
    return "build_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


class KnowledgeBuildTransaction(BaseModel):
    """一次 Wiki 构建事务的持久化状态机。"""

    transaction_id: str = Field(description="稳定事务 ID（优秀复用 build_id）")
    build_id: str = Field(description="幂等 build_id")
    source_snapshot_id: str = Field(description="pending SourceSnapshot id")
    parent_source_snapshot_id: str | None = Field(default=None)
    source_snapshot_hash: str = Field(default="")
    parent_wiki_version: str = Field(default="")
    compiler_version: str = Field(default="")
    schema_version: int = Field(default=1)
    staging_path: str = Field(default="")
    wiki_version: str = Field(default="")
    state: BuildState = Field(default="DISCOVERED")
    started_at: str = Field(default="")
    updated_at: str = Field(default="")
    failure_reason: str = Field(default="")
    retry_count: int = Field(default=0, ge=0)

    @classmethod
    def begin(
        cls,
        *,
        snapshot: object,
        parent_wiki_version: str = "",
        compiler_version: str = "deterministic-1",
        schema_version: int = 1,
    ) -> KnowledgeBuildTransaction:
        """由 pending SourceSnapshot 开启一个新事务（DISCOVERED）。"""
        snap_hash = getattr(snapshot, "snapshot_hash", "")
        parent_snap = getattr(snapshot, "parent_snapshot_id", None)
        build_id = compute_build_id(
            parent_wiki_version=parent_wiki_version,
            source_snapshot_hash=snap_hash,
            compiler_version=compiler_version,
            schema_version=schema_version,
        )
        now = datetime.now(UTC).isoformat()
        return cls(
            transaction_id=build_id,
            build_id=build_id,
            source_snapshot_id=getattr(snapshot, "snapshot_id", build_id),
            parent_source_snapshot_id=parent_snap,
            source_snapshot_hash=snap_hash,
            parent_wiki_version=parent_wiki_version,
            compiler_version=compiler_version,
            schema_version=schema_version,
            state="DISCOVERED",
            started_at=now,
            updated_at=now,
        )

    def transition(self, new_state: BuildState, *, reason: str = "") -> None:
        """校验并推进状态机；非法迁移抛 ValueError，幂等迁移忽略。"""
        if new_state == self.state:
            return
        allowed = _ALLOWED_TRANSITIONS.get(self.state, set())
        if new_state not in allowed:
            raise ValueError(f"非法事务迁移 {self.state} -> {new_state} (tx={self.transaction_id})")
        self.state = new_state
        self.updated_at = datetime.now(UTC).isoformat()
        if new_state == "COMPILING":
            self.retry_count += 1
        if reason:
            if new_state == "FAILED":
                self.failure_reason = reason
            else:
                self.failure_reason = ""

    def record(self, *, wiki_version: str = "", staging_path: str = "") -> None:
        if wiki_version:
            self.wiki_version = wiki_version
        if staging_path:
            self.staging_path = staging_path
        self.updated_at = datetime.now(UTC).isoformat()

    def recovery_action(self) -> str:
        return RECOVERY_ACTIONS.get(self.state, "NOOP")


class TransactionStore:
    """事务的磁盘持久化（原子写）与 Crash Recovery 扫描。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, transaction_id: str) -> Path:
        return self.root / f"{transaction_id}.json"

    def save(self, tx: KnowledgeBuildTransaction) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path(tx.transaction_id)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(tx.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)

    def load(self, transaction_id: str) -> KnowledgeBuildTransaction | None:
        path = self._path(transaction_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return KnowledgeBuildTransaction.model_validate(data)
        except Exception as exc:
            logger.error("事务加载失败 %s: %s", transaction_id, exc)
            return None

    def list_unfinished(self) -> list[KnowledgeBuildTransaction]:
        out: list[KnowledgeBuildTransaction] = []
        if not self.root.is_dir():
            return out
        for fp in self.root.glob("*.json"):
            tx = self.load(fp.stem)
            if tx is not None and tx.state not in TERMINAL_STATES:
                out.append(tx)
        return sorted(out, key=lambda t: t.started_at)

    def recovery_plan(self) -> list[dict]:
        """扫描未完成事务并给出每个事务建议的 Recovery 动作。"""
        plan: list[dict] = []
        for tx in self.list_unfinished():
            plan.append(
                {
                    "transaction_id": tx.transaction_id,
                    "build_id": tx.build_id,
                    "state": tx.state,
                    "action": tx.recovery_action(),
                    "retry_count": tx.retry_count,
                }
            )
        return plan
