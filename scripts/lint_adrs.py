"""Validate required Architecture Decision Record fields."""

from __future__ import annotations

import logging
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = REPO_ROOT / "docs" / "agent-full-loop" / "adr"
REQUIRED_HEADINGS = ("Status", "Date", "Decision", "Alternatives", "Consequences")


def validate_adr(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    for heading in REQUIRED_HEADINGS:
        marker = f"## {heading}"
        if marker not in text:
            errors.append(f"{path}: missing {marker}")
            continue
        body = text.split(marker, 1)[1].split("\n## ", 1)[0].strip()
        if not body:
            errors.append(f"{path}: empty {marker}")
    return errors


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    files = sorted(ADR_DIR.glob("*.md"))
    errors = [error for path in files for error in validate_adr(path)]
    if not files:
        errors.append(f"no ADR files found in {ADR_DIR}")
    if errors:
        raise SystemExit("\n".join(errors))
    logging.info("validated %d ADR file(s)", len(files))


if __name__ == "__main__":
    main()
