"""CLI entry point used by CI to validate stage-0 full-loop assets."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "services" / "backend"))

from tests.agent_evals.full_loop_journeys.schema import (  # noqa: E402
    load_dataset,
    validate_dataset,
)


def main() -> None:
    print(json.dumps(validate_dataset(load_dataset()), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
