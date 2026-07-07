/**
 * PR Agent Dashboard — TypeScript 类型定义
 *
 * 与后端 REST API 响应结构保持一致。
 */

// ═══════════════════════════════════════════════════════════
// 基础类型
// ═══════════════════════════════════════════════════════════

export type SourceType = "overseas_news" | "wechat_mp" | "paper";

export type PipelinePhase = "crawl" | "classify" | "score" | "report";

export type PipelineStatus =
  | "idle"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

// ═══════════════════════════════════════════════════════════
// Article（文章）
// ═══════════════════════════════════════════════════════════

export interface Article {
  _id: string;
  url_hash: string;
  title: string;
  url: string;
  source: string;
  source_type: SourceType;
  published_at: string;
  added_at: string;
  summary: string;
  summary_cn: string;
  content_md?: string;
  is_ai_security: boolean;
  is_agent_security: boolean;
  category: string;
  // V2 6分类
  category_v2?: string;
  category_v2_confidence?: number;
  category_v2_reason?: string;
  category_v2_fallback?: boolean;
  is_pr_eligible?: boolean;
  // V2 双维度评分
  product_relevance?: number;
  event_impact?: number;
  pr_total_score?: number;
  // V2 PR 草稿
  pr_drafts?: DraftItem[];
  pr_template_used?: string;
  // V1 评分（保留兼容）
  ai_relevance_score: number;
  reportability_score: number;
  total_score: number;
  is_high_value: boolean;
  score_reason?: string;
  has_report: boolean;
  report_id: string | null;
  pipeline_status?: string;
}

export interface ArticleQuery {
  page?: number;
  page_size?: number;
  source_type?: SourceType;
  category?: string;
  min_score?: number;
  is_ai_security?: boolean;
  is_high_value?: boolean;
  keyword?: string;
  sort_by?: string;
  order?: "asc" | "desc";
}

// ═══════════════════════════════════════════════════════════
// Pagination（分页）
// ═══════════════════════════════════════════════════════════

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

// ═══════════════════════════════════════════════════════════
// Stats（统计）
// ═══════════════════════════════════════════════════════════

export interface StatsData {
  total_articles: number;
  ai_security_count: number;
  high_value_count: number;
  source_distribution: Record<string, number>;
  category_distribution: Record<string, number>;
}

// ═══════════════════════════════════════════════════════════
// Report（PR 报道）
// ═══════════════════════════════════════════════════════════

export interface Report {
  _id: string;
  article_url_hash: string;
  title: string;
  content_md: string;
  template: string;
  scores: {
    relevance: number;
    reportability: number;
  };
  generated_by: string;
  created_at: string;
}

// ═══════════════════════════════════════════════════════════
// Draft（V2 PR 草稿）
// ═══════════════════════════════════════════════════════════

export interface DraftItem {
  template: string;
  perspective: string;
  content_md: string;
  title: string;
  index: number;
}

// ═══════════════════════════════════════════════════════════
// Pipeline（流水线）
// ═══════════════════════════════════════════════════════════

export interface PipelineState {
  crawl_days: number;
  phases: PipelinePhase[];
  crawled_count: number;
  classified_count: number;
  scored_count: number;
  report_count: number;
  errors: string[];
  status: PipelineStatus;
  current_phase: string;
  started_at: string;
  finished_at: string;
}

export interface PipelineResult {
  pipeline_id: string;
  status: PipelineStatus;
  state: PipelineState;
}

export interface PipelineStatusResponse {
  status: PipelineStatus;
  current_phase: string;
  state: PipelineState;
  errors: string[];
}

// ═══════════════════════════════════════════════════════════
// Knowledge（知识库）
// ═══════════════════════════════════════════════════════════

export interface KnowledgeSummary {
  loaded: boolean;
  message?: string;
  product_name?: string;
  features_count?: number;
  barriers_count?: number;
  control_points_count?: number;
  cases_count?: number;
  keywords?: string[];
  loaded_at?: string;
}

// ═══════════════════════════════════════════════════════════
// Filter（筛选栏状态）
// ═══════════════════════════════════════════════════════════

export interface FilterValues {
  source_type?: SourceType;
  category?: string;
  min_score?: number;
  keyword?: string;
  is_high_value?: boolean;
}

// ═══════════════════════════════════════════════════════════
// WeWe Account（公众号账号管理）
// ═══════════════════════════════════════════════════════════

export interface WeWeAccount {
  id: string;
  name: string;
  status: string;        // active / expired / disabled
  vid?: string;
  token?: string;
  last_login?: string;
}

export interface AccountStatusResult {
  ok: boolean;
  accounts?: WeWeAccount[];
  total?: number;
  active_count?: number;
  message?: string;
}

export interface QRCodeResult {
  ok: boolean;
  uuid?: string;
  qr_base64?: string;    // base64 PNG (from backend)
  qrcode_img?: string;   // alias
  scan_url?: string;
  message?: string;
}

export interface PollLoginResult {
  ok: boolean;
  status?: string;        // waiting / scanned / confirmed / expired
  vid?: string;
  token?: string;
  name?: string;
  message?: string;
}

// ═══════════════════════════════════════════════════════════
// Chat（对话改稿）
// ═══════════════════════════════════════════════════════════

export type ChatRole = "user" | "assistant";

export interface ChatMessage {
  role: ChatRole;
  content: string;
}

export interface ChatAskRequest {
  message: string;
  article_url_hash?: string;
  draft_index?: number;
  revision_id?: string;
  history?: ChatMessage[];
}

export interface ChatAskResponse {
  answer: string;
  references: string[];
}

// ═══════════════════════════════════════════════════════════
// Draft Revision（草稿修订）
// ═══════════════════════════════════════════════════════════

export interface DraftRevision {
  revision_id: string;
  instruction: string;
  content_md: string;
  change_summary: string[];
  created_at: string;
  created_by: string;
  applied: boolean;
}

export interface DraftReviseRequest {
  instruction: string;
  save?: boolean;
}

export interface DraftReviseResponse {
  revision_id: string;
  revised_content_md: string;
  change_summary: string[];
  saved: boolean;
}

export interface ApplyRevisionResponse {
  article_url_hash: string;
  draft_index: number;
  revision_id: string;
  applied: boolean;
}
