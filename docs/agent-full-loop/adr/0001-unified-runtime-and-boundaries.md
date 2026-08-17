# ADR-0001: Unified Conversation Runtime And Business Boundaries

## Status

Accepted

## Date

2026-08-16

## Context

The repository has a bounded `AgentLoop`, an autonomous `AgentRuntime`, fixed pipeline DAGs,
and direct chat/draft endpoints. Keeping these as independent product runtimes would make
cross-turn state, approvals, replay, policy and recovery inconsistent.

## Decision

1. `AgentRuntime` is the single long-term production execution state machine. Existing
   Chat Loop and pipeline entry points remain compatibility adapters while callers migrate.
2. Tools represent stable business actions, not database collections or internal functions.
   Side effects use L0 read-only, L1 recoverable write, L2 confirmed business write and L3
   prohibited/external publication levels.
3. Cross-turn state is stored through `TaskStateStore`; runtime checkpoints remain in
   `RuntimeStateStore`. Both use explicit schema versions and optimistic concurrency.
4. Saving a new draft version is L2 unless an explicit scoped policy grants it. Publishing
   is not implemented as an autonomous stage-1 action and always requires separate approval.
5. Skills, prompts, models, knowledge and tool registries are frozen by `RunManifest`.
   Runtime code may propose candidates but may not mutate production Skills directly.
6. Existing button entry points remain expert shortcuts and rollback paths. They may not
   bypass tenant scope, Policy or versioned Tool contracts when migrated to the new runtime.

## Alternatives

- Keep Chat Loop and Autonomous Runtime permanently separate. Rejected because state,
  approvals, event schemas and recovery behavior would diverge.
- Expose database and internal service functions directly as Tools. Rejected because it
  expands the permission surface and couples plans to storage implementation.
- Replace all existing button endpoints in one release. Rejected because it removes the
  rollback path before journey and domain-quality gates are established.

## Consequences

- New full-loop behavior must enter through `TaskEnvelope`, slot policy, Runtime Policy and
  versioned Tool contracts.
- Compatibility adapters are temporary duplication and must emit the shared event contract.
- MongoDB writes require tenant/user scope plus idempotency or optimistic locking.
- Release remains blocked on dual human domain-quality annotations; engineering metrics alone
  cannot establish subjective copy quality.
