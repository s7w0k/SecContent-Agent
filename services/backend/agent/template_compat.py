"""Read-time compatibility for drafts and reports created before template versioning."""

from __future__ import annotations

from typing import Any


def normalize_legacy_template_metadata(item: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with stable fallback metadata while preserving legacy content."""
    normalized = dict(item)
    name = str(normalized.get("template") or "unknown")
    legacy_id = f"legacy:{name}"
    normalized.setdefault("template_id", legacy_id)
    normalized.setdefault("template_key", legacy_id)
    normalized.setdefault("template_version", 0)
    normalized.setdefault("template_source", "legacy")
    normalized.setdefault(
        "template_snapshot",
        {
            "template_key": normalized["template_key"],
            "category_v2": "",
            "slot": "",
            "name": name,
            "title_template": "",
            "sections": [],
            "perspectives": [normalized["perspective"]] if normalized.get("perspective") else [],
            "perspective": normalized.get("perspective", ""),
            "extra_instructions": "",
            "system_version": 0,
            "legacy": True,
        },
    )
    return normalized


def normalize_legacy_drafts(drafts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize a draft list without mutating the MongoDB document."""
    return [normalize_legacy_template_metadata(draft) for draft in drafts]


def template_reference(item: dict[str, Any] | None) -> dict[str, Any]:
    """Extract stable, content-free template metadata from a draft or report."""
    if not item:
        return {}
    normalized = normalize_legacy_template_metadata(item)
    return {
        "template_id": normalized["template_id"],
        "template_key": normalized["template_key"],
        "template_version": normalized["template_version"],
        "template_name": str(normalized.get("template") or "unknown"),
    }
