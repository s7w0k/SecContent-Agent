from __future__ import annotations

import pytest
from agent.contracts.task import SlotSource, SlotState, TaskEnvelope, TaskIntent
from agent.run_manifest import (
    ManifestError,
    RunManifestStore,
    build_run_manifest,
    manifest_fingerprint,
)
from agent.runtime_state import ArtifactReference, RuntimeState, migrate_runtime_state


def _envelope() -> TaskEnvelope:
    return TaskEnvelope.from_user_input(
        task_id="run-1",
        thread_id="thread-1",
        user_id="user-1",
        tenant_id="tenant-1",
        goal="生成稿件",
        intent=TaskIntent.GENERATE_DRAFT,
        acceptance_criteria=["生成初稿"],
    )


def test_runtime_state_persists_task_slots_questions_and_artifacts():
    envelope = _envelope()
    envelope = envelope.model_copy(
        update={"selected_article_ids": SlotState.from_value(["art-1"], source=SlotSource.USER)}
    )
    state = RuntimeState(
        run_id="run-1",
        thread_id="thread-1",
        user_id="user-1",
        tenant_id="tenant-1",
        goal="生成稿件",
        task_envelope=envelope,
        slot_states=envelope.slot_states(),
        current_turn_id="turn-2",
        pending_questions=["是否保存？"],
        artifact_refs=[
            ArtifactReference(
                artifact_id="draft-1", artifact_type="draft", content_hash="sha256:abc"
            )
        ],
    )
    restored = migrate_runtime_state(state.model_dump(mode="json"))
    assert restored.task_envelope == envelope
    assert restored.slot_states["selected_article_ids"].value == ["art-1"]
    assert restored.artifact_refs[0].artifact_id == "draft-1"
    assert restored.pending_questions == ["是否保存？"]


def test_manifest_freezes_task_and_input_versions():
    envelope = _envelope()
    manifest = build_run_manifest(
        run_id="run-1",
        user_id="user-1",
        tenant_id="tenant-1",
        code_revision="commit-abc",
        tool_registry_version="tools-1",
        task_schema_version=envelope.schema_version,
        task_snapshot_hash=envelope.fingerprint(),
        slot_snapshot_hash=envelope.slot_fingerprint(),
        plan_version=3,
        input_refs=["article:art-1@v2"],
    )
    assert manifest.task_snapshot_hash.startswith("sha256:")
    assert manifest.slot_snapshot_hash.startswith("sha256:")
    assert manifest.plan_version == 3
    assert manifest.input_refs == ["article:art-1@v2"]
    assert manifest_fingerprint(manifest) == manifest_fingerprint(manifest.model_copy())


async def test_manifest_store_rejects_mutation_after_freeze():
    class _Collection:
        def __init__(self):
            self.docs = []

        async def find_one(self, query):
            return next(
                (doc for doc in self.docs if doc.get("run_id") == query.get("run_id")), None
            )

        async def replace_one(self, query, doc, upsert=False):
            assert upsert is True
            self.docs.append(doc)

    class _DB(dict):
        def __getitem__(self, key):
            if key not in self:
                self[key] = _Collection()
            return super().__getitem__(key)

    store = RunManifestStore(_DB())
    original = build_run_manifest(
        run_id="run-immutable",
        user_id="user-1",
        code_revision="commit-1",
        tool_registry_version="tools-1",
    )
    await store.save(original)
    await store.save(original)
    with pytest.raises(ManifestError, match="already frozen"):
        await store.save(original.model_copy(update={"model_id": "changed"}))
