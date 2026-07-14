"""LangGraph MongoDB checkpointer integration."""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.mongodb import MongoDBSaver
from pymongo import MongoClient


def supports_mongodb_checkpoints(db: Any) -> bool:
    """Return whether ``db`` exposes a real Motor/PyMongo client."""
    motor_client = getattr(db, "client", None)
    sync_client = getattr(motor_client, "delegate", motor_client)
    return isinstance(sync_client, MongoClient) and isinstance(getattr(db, "name", None), str)


def create_checkpointer(
    db: Any,
    collection_name: str = "pipeline_checkpoints",
    writes_collection_name: str = "pipeline_checkpoint_writes",
) -> MongoDBSaver:
    """Create a saver backed by the PyMongo client underneath a Motor database.

    ``MongoDBSaver`` is synchronous internally and exposes async wrappers that
    run its operations in an executor. Motor uses a thread-safe PyMongo client
    as its ``delegate``, so both layers can safely share the existing pool.
    """
    motor_client = getattr(db, "client", None)
    sync_client = getattr(motor_client, "delegate", motor_client)
    db_name = getattr(db, "name", None)
    if not supports_mongodb_checkpoints(db):
        raise TypeError("A connected Motor or PyMongo database is required")

    return MongoDBSaver(
        sync_client,
        db_name=db_name,
        checkpoint_collection_name=collection_name,
        writes_collection_name=writes_collection_name,
    )
