# Full-Loop Agent Stage 0 Baseline

Status: frozen engineering baseline v1  
Date: 2026-08-16  
Scope: UI buttons through `/api/pipeline/*`, `/api/chat/*` and `/api/autonomous/*`

## Evidence

- API and persistence snapshots:
  `tests/agent_evals/full_loop_journeys/legacy_contract_snapshots.v1.json`
- Full-loop journeys: `tests/agent_evals/full_loop_journeys/dataset.v1.jsonl`
  (65 cases, 13 categories, 5 per category)
- Legacy chat baseline: `reports/legacy-baseline-stage0.json` (40 cases x 3 runs)
- Paired Agent evaluation: `reports/eval-harness-pr-real_v1.json` (155 evaluations)
- Domain baseline status: `reports/full-loop-domain-quality-legacy.v1.json`

The mock-backed engineering baseline is reproducible offline. It is not evidence of
subjective copy quality. Classification, product matching, expert score ranges, draft quality
and revision preservation still require two human reviewers before release thresholds freeze.

## Existing UI Call Chains

| UI action | Client/API | Service chain | Persistent effects | Retry/error behavior |
|---|---|---|---|---|
| Full pipeline | `PipelineControl` -> `pipelineApi.runV2` -> `POST /api/pipeline/run-v2` | task creation -> ARQ -> `pipeline_v2` DAG | `pipeline_tasks`, `articles`, `article_assessments`, `user_drafts`, review/log collections | 503 when DB/queue is absent; checkpoints resume nodes; task creation has no client idempotency key |
| Crawl | `PipelineControl` -> `crawl`/`crawlOverseas`/`crawlWewe` | ingestion service or feed adapter -> classifier | `pipeline_tasks`, `crawl_runs`, `articles` | source errors map mainly to 502; URL/content keys deduplicate articles |
| Classify | `PipelineControl`/article action -> `classify-v2` | `ClassifierV2` -> article update | `articles`, `pipeline_locks`, `execution_logs` | existing derived fields and short-lived lock suppress duplicate LLM calls |
| Score | `PipelineControl`/article action -> `score-v2` | `ScoringAgentV2`, product catalog/matcher -> assessment update | `articles`, `article_assessments`, `pipeline_locks`, `execution_logs` | existing complete score is reused unless forced; conflicts/timeouts are explicit |
| Generate article draft | article action -> `run-v2/{url_hash}` | task worker -> classify/match/score/generate/review nodes | `pipeline_tasks`, `user_drafts`, review/log collections | node checkpoints recover; draft version semantics are currently embedded in pipeline code |
| Ask about article/draft | `ChatPage` -> `chatApi.askStream` -> `/api/chat/ask_stream` | `DraftChatAgent` -> read-only Agent tools/context | `chat_sessions`, operation/activity logs | model/provider failures become SSE error or 502; duplicate messages are not request-idempotent |
| Revise draft | `ChatPage` -> `reviseDraftStream` | `DraftChatAgent.revise` -> optional review/save | `user_drafts.revisions`, `chat_sessions`, `activity_logs`, `memory_events` | each successful saved retry creates a new revision id |
| Apply revision | `ChatPage` -> `applyRevision` | revision lookup -> overwrite selected draft content | `user_drafts`, `activity_logs`, `memory_events` | repeated value is effectively idempotent but repeats secondary audit attempts |
| Review draft | `ChatPage` -> `reviewDraft` | `DraftReviewer.review` | `user_drafts.drafts[n].review` | derived review overwrites only review data and is tied to content hash |
| Autonomous run | `PipelineControl` -> `autonomousApi.createRun` | `AutonomousRunService` -> `AgentRuntime` -> policy/executor/validator | `runtime_manifests`, `runtime_runs`, `runtime_events`, outbox, approvals, ledger | bounded retry/budget; checkpoint/CAS recovery; approvals are one-time scoped tokens |

## Storage And Side Effects

| Collection | Owner | Effect | Current protection |
|---|---|---|---|
| `articles` | crawl/classify/score pipeline | source article plus derived classification and scores | URL hash, field completeness checks, article locks |
| `article_assessments` | scoring | derived assessment/version evidence | article/model/config identifiers |
| `pipeline_tasks` | pipeline API/worker | durable background task and checkpoint state | `task_id`, owner filter, lease/checkpoint logic |
| `user_drafts` | generation/chat | draft arrays, revisions and reviews | `user_id + article_url_hash`; inconsistent version/idempotency semantics |
| `chat_sessions` | chat API | user-visible turns | user/article/draft scope; no general task envelope |
| `memory_events` | chat revision/apply | preference learning candidates | explicit idempotency keys |
| `runtime_runs` | autonomous runtime | mutable execution state | run id, checkpoint version CAS, terminal-state guard |
| `runtime_manifests` | autonomous runtime | immutable version/input manifest | run id and frozen model/code/tool/knowledge references |
| `runtime_events` / `agent_run_events` | autonomous/Agent Loop | SSE and audit events | run sequence, TTL; shared base contract added in stage 1 |
| `conversation_tasks` | full-loop conversation runtime | task envelope, slots, turns and runtime checkpoint reference | tenant/user scope, CAS version, TTL (stage 1) |

Direct Mongo access still exists inside legacy APIs and business services. The ADR prohibits
exposing these collections to Agent tools; stage 2 adapters must wrap the service layer.

## Current Error Semantics

- 401: authentication failure.
- 404: scoped article/draft/run/revision not found; autonomous lookup filters by user.
- 409: lock, lifecycle, approval or optimistic concurrency conflict.
- 422: invalid request/enum/range or unauthorized tool chain.
- 502: model/source/service execution failure.
- 503: database, queue, model component or runtime is unavailable.
- 504: bounded pipeline lock wait expired.

The legacy pipeline and chat paths sometimes return free-text `detail`; new full-loop code must
add stable `reason_code` values without removing existing status codes during migration.

## Frozen Engineering Metrics

| Metric | Legacy | Candidate | Interpretation |
|---|---:|---:|---|
| Legacy chat success | 1.000 (120 mock requests) | n/a | harness/process baseline only |
| Legacy chat fact support | 1.000 | n/a | deterministic keyword check |
| Paired evaluation success | 0.871 | 0.8387 | candidate currently regresses execution success |
| Deterministic pass rate | 0.9821 | 1.000 | candidate improves deterministic contract checks |
| Quality-regressed cases | n/a | 0 | no regression under current deterministic rubric |

No absolute human-quality target is asserted. The release rule is non-regression against the
frozen measured baseline plus explicit improvement goals after dual review is complete.

## Rollback Baseline

The existing Pipeline and Chat buttons remain enabled. New conversation orchestration must be
feature-flagged or shadowed and can be disabled without deleting these endpoints. Existing
snapshots are the compatibility contract during stages 2-12.
