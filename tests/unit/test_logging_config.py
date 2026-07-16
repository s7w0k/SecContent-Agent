"""Logging rotation regression tests."""

from __future__ import annotations

import gzip
from pathlib import Path

from logging_config import _compress_rotated_log


def test_rotator_compresses_source_to_timestamped_destination(tmp_path: Path) -> None:
    source = tmp_path / "app.log"
    destination = tmp_path / "app.log.2026-07-16"
    source.write_text("structured log\n", encoding="utf-8")

    _compress_rotated_log(str(source), str(destination))

    archive = tmp_path / "app.log.2026-07-16.gz"
    assert not source.exists()
    assert archive.exists()
    with gzip.open(archive, "rt", encoding="utf-8") as handle:
        assert handle.read() == "structured log\n"


def test_rotator_ignores_a_missing_source(tmp_path: Path) -> None:
    _compress_rotated_log(
        str(tmp_path / "missing.log"),
        str(tmp_path / "missing.log.2026-07-16"),
    )

    assert list(tmp_path.iterdir()) == []
