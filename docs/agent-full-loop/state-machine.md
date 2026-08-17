# Full-Loop Task And Runtime State Model

## Task State

`TaskStateStore` owns the durable cross-turn task record:

```text
pending -> waiting_user -> planning -> running -> completed
                     \-> waiting_approval -> running
running -> retrying -> running
running -> degraded -> completed | failed
non-terminal -> canceled | stopped | failed
```

Terminal task states are `completed`, `failed`, `canceled` and `stopped`. Active queries are
always filtered by both `tenant_id` and `user_id`. A successful mutation increments `version`;
an outdated writer receives `TaskStateConflictError` and must reload explicitly.

## Runtime State

`RuntimeState` retains execution details and now includes an immutable `TaskEnvelope` snapshot,
current slot states, current turn, pending questions, plan version and artifact references.
Runtime checkpoints remain in `runtime_runs` and use `checkpoint_version` CAS. A Task checkpoint
links to `run_id + runtime_checkpoint_version`, so a service restart loads the task first and
then the exact runtime state without embedding model prompts or private reasoning.

## Version Freeze

`RunManifest` freezes code, model, prompt, skill, tool registry and knowledge versions together
with task schema/input hash, slot hash, plan version and input references. The manifest is
immutable after run start.

## Event Ordering

All runtime modes share `EventEnvelope` fields:
`schema_version`, `event_id`, `run_id`, `turn_id`, `trace_id`, `sequence`, `event_type`,
`status`, `timestamp` and a redacted summary payload. `run_id + sequence` is unique. Optional
deduplication keys make repeated delivery read-idempotent; replay validation reports duplicates,
sequence gaps and out-of-order records before rebuilding the user timeline.
