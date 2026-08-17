# Full-Loop Business Tool Source Catalog

Date: 2026-08-16  
ADR: `docs/agent-full-loop/adr/0001-unified-runtime-and-boundaries.md`

| Target Tool | Side effect | Reuse source | Current shape | Stage-0 disposition |
|---|---:|---|---|---|
| `search_news` | L0 | web search service/API and existing article queries | service plus direct API calls | wrap |
| `crawl_news` | L1 | `OverseasNewsIngestionService`, MCP crawl adapters | service writes articles/crawl runs | wrap with idempotency key |
| `list_articles` | L0 | article API/repository queries, current chat tool | some direct Mongo queries | wrap |
| `get_article` | L0 | current `agent_tools.get_article` and article lookup | read-only pseudo Tool exists | reuse then standardize contract |
| `select_article_candidates` | L0 | pipeline filters/scoring order | logic spread across API/DAG | refactor into deterministic service |
| `classify_article` | L1 | `ClassifierV2` plus article lock/update path | service and API orchestration | wrap |
| `match_products` | L0 | `ProductMatcher`, catalog/routing services | callable service | reuse behind Tool adapter |
| `score_article` | L1 | `ScoringAgentV2`, assessment service | service plus API persistence | wrap |
| `generate_draft` | L1 | draft generator and `pipeline_v2.draft_node` | persistence mixed with generation | refactor boundary, then wrap |
| `review_draft` | L1 | `DraftReviewer` and review helper | content-hash derived write | reuse behind Tool adapter |
| `revise_draft` | L1 | `DraftChatAgent.revise` | revision creation mixed into API | refactor boundary, then wrap |
| `save_draft_version` | L2 | `user_drafts` revision/apply code | direct array replacement, mixed idempotency | refactor with immutable versions/CAS |
| `export_draft` | L1 | frontend download/client formatting | no canonical backend artifact Tool | build later |
| `publish_draft` | L3 | none | not a supported autonomous action | prohibited candidate |

## Existing Tool Classification

- Reuse: Tool Harness, policy, budget, goal validator, RuntimeStateStore, RunManifest, current
  read-only article/knowledge/memory tools, product matcher and reviewer services.
- Wrap: ingestion, article reads, classifier, scorer and deterministic pipeline subflows.
- Refactor: candidate selection, generation persistence, revision/save version semantics and
  legacy direct Mongo orchestration.
- Deprecation candidates after migration: demo deterministic autonomous planner/executor,
  duplicate event envelope definitions, chat-specific state as the sole cross-turn store and
  direct feed ingestion embedded in API routes.

Every future Tool contract must point to one row above, declare input/output schema,
`reason_code`, source ids, side-effect level and idempotency behavior.
