# Full-Loop Journey Dataset v1

`dataset.v1.jsonl` contains 65 privacy-safe cases across 13 required journey categories.
Each row declares conversation turns, initial state, expected slots and provenance, allowed and
forbidden tools, expected clarification, acceptance criteria and acceptable terminal states.

Validate locally with:

```bash
python -m tests.agent_evals.full_loop_journeys.validate_dataset
pytest tests/unit/test_full_loop_stage0.py tests/unit/test_task_contracts.py
```

`legacy_contract_snapshots.v1.json` freezes representative request/response shapes and Mongo
before/after effects for current Pipeline, Chat and Autonomous button paths.

The engineering dataset contains no assertion that two humans reviewed subjective output.
Before release, two reviewers must populate `ReviewerAnnotation` records from
`agent.harness.domain_quality_baseline`, then freeze agreement and aggregate metrics in a new
versioned report. Until then, `reports/full-loop-domain-quality-legacy.v1.json` deliberately
marks those metrics pending.
