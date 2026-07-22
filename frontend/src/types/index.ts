/**
 * PR Agent Dashboard — TypeScript 类型定义
 *
 * 与后端 REST API 响应结构保持一致。
 */

// ═══════════════════════════════════════════════════════════
// 基础类型
// ═══════════════════════════════════════════════════════════

export type SourceType = 'overseas_news' | 'wechat_mp' | 'paper' | 'user_upload';

export type PipelinePhase = 'crawl' | 'classify' | 'score' | 'report';

export type PipelineStatus = 'idle' | 'running' | 'completed' | 'failed' | 'cancelled';

// ═══════════════════════════════════════════════════════════
// Authentication（用户认证）
// ═══════════════════════════════════════════════════════════

export interface User {
  user_id: string;
  username: string;
  display_name: string;
  email?: string | null;
  is_developer: boolean;
  created_at: string;
}

export interface AuthResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: User;
}

export interface LoginRequest {
  username: string;
  password: string;
}

export interface RegisterRequest extends LoginRequest {
  display_name?: string;
  email?: string;
}

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
  is_ai_agent_security_relevant?: boolean;
  ai_agent_security_relevance_confidence?: number;
  ai_agent_security_relevance_reason?: string;
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
  order?: 'asc' | 'desc';
}

export interface UploadArticleResult {
  url_hash: string;
  title: string;
  source_type: 'user_upload';
  content_length: number;
  message: string;
}

export interface EffectivePrompt {
  prompt_key: string;
  content: string;
  is_custom: boolean;
  required_placeholders: string[];
  updated_at?: string | null;
}

/** 热点排行文章条目，仅包含排行面板需要的字段。 */
export interface HotArticle {
  url_hash: string;
  title: string;
  url: string;
  pr_total_score: number;
  category_v2: string;
  added_at: string;
  source_type: SourceType;
}

export interface HotRankingQuery {
  limit?: number;
  category?: string;
  date_range?: '1d' | '7d' | '30d' | 'all';
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
  today_count: number;
  today_ai_security_count: number;
  today_high_value_count: number;
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
  template_id?: string;
  template_key?: string;
  template_version?: number;
  template_source?: 'system' | 'user' | 'legacy';
  perspective: string;
  content_md: string;
  title: string;
  index: number;
  revisions?: DraftRevision[];
  feedback_summary?: {
    avg_rating: number;
    count: number;
    last_rating?: number | null;
  };
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
  classified_v2_count?: number;
  pr_eligible_count?: number;
  scored_v2_count?: number;
  draft_count?: number;
  score_threshold?: number;
  threshold_adjustment?: number;
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

export type PipelineTaskStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'interrupted';

export interface PipelineTaskProgress {
  phase: string;
  current: number;
  total: number;
  message: string;
}

export interface PipelineTask {
  id?: string;
  task_id: string;
  user_id: string;
  task_type: 'crawl' | 'classify' | 'classify-v2' | 'score' | 'score-v2' | 'run-v2' | 'report';
  article_url_hash?: string | null;
  status: PipelineTaskStatus;
  progress: PipelineTaskProgress;
  result?: Record<string, unknown> | null;
  error?: string | null;
  created_at: string;
  updated_at: string;
  expires_at: string;
}

export interface PipelineTaskList {
  items: PipelineTask[];
  total: number;
  page: number;
  page_size: number;
}

export interface PipelineTaskCreated {
  task_id: string;
  message: string;
  total?: number;
}

export interface PipelineTaskResponse {
  ok: boolean;
  data: PipelineTaskCreated;
}

export interface PipelineLogEntry {
  level: string;
  phase: string;
  message: string;
  created_at: string;
}

export interface PipelineLogsResponse {
  logs: PipelineLogEntry[];
  phases: string[];
}

export interface DevLogError {
  type?: string;
  message?: string;
  stack_trace?: string;
  [key: string]: unknown;
}

export interface DevLogEntry {
  _id?: string;
  log_id: string;
  trace_id?: string | null;
  user_id: string;
  username?: string | null;
  level: string;
  phase: string;
  action: string;
  message: string;
  detail: Record<string, unknown>;
  duration_ms?: number | null;
  error?: DevLogError | null;
  created_at: string;
  date: string;
}

export interface TraceEvent extends DevLogEntry {}

export interface DevLogUserOption {
  user_id: string;
  username: string;
}

export interface DevLogQuery {
  date?: string;
  user_id?: string;
  phase?: string[];
  level?: string[];
  trace_id?: string;
  keyword?: string;
  page?: number;
  page_size?: number;
}

export interface DevLogQueryResult {
  logs: DevLogEntry[];
  phases: string[];
  levels: string[];
  users: DevLogUserOption[];
  total: number;
  page: number;
  page_size: number;
}

export interface DevLogTrace {
  trace_id: string;
  user_id: string;
  username: string;
  events: TraceEvent[];
  total_duration_ms: number;
  phase_count: number;
  has_error: boolean;
}

export interface DevLogStats {
  total: number;
  by_level: Record<string, number>;
  by_phase: Record<string, number>;
  by_user: Array<DevLogUserOption & { count: number }>;
  error_count: number;
  avg_duration_ms: Record<string, number>;
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
  status: string; // active / expired / disabled
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
  qr_base64?: string; // base64 PNG (from backend)
  qrcode_img?: string; // alias
  scan_url?: string;
  message?: string;
}

export interface PollLoginResult {
  ok: boolean;
  status?: string; // waiting / scanned / confirmed / expired
  vid?: string;
  token?: string;
  name?: string;
  message?: string;
}

// ═══════════════════════════════════════════════════════════
// Chat（对话改稿）
// ═══════════════════════════════════════════════════════════

export type ChatRole = 'user' | 'assistant';

export interface ChatMessage {
  role: ChatRole;
  content: string;
  created_at?: string;
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

// ═══════════════════════════════════════════════════════════
// Feedback（用户反馈）
// ═══════════════════════════════════════════════════════════

export type TargetType = 'draft' | 'revision' | 'article_score' | 'pipeline';

export type FeedbackStatus = 'active' | 'archived';

export interface FeedbackTargetRef {
  article_url_hash: string;
  draft_index?: number;
  revision_id?: string;
  pipeline_id?: string;
}

export interface Feedback {
  feedback_id: string;
  user_id: string;
  template_id?: string | null;
  template_key?: string | null;
  template_version?: number | null;
  template_name?: string | null;
  perspective?: string | null;
  target_type: TargetType;
  target_ref: FeedbackTargetRef;
  rating: number;
  rating_dimensions?: Record<string, number> | null;
  comment: string;
  tags: string[];
  status: FeedbackStatus;
  created_at: string;
  updated_at: string;
}

export interface FeedbackCreate {
  target_type: TargetType;
  target_ref: FeedbackTargetRef;
  rating: number;
  rating_dimensions?: Record<string, number>;
  comment?: string;
  tags?: string[];
}

export interface FeedbackUpdate {
  rating?: number;
  rating_dimensions?: Record<string, number>;
  comment?: string;
  tags?: string[];
  status?: FeedbackStatus;
}

export interface FeedbackQuery {
  target_type?: TargetType;
  article_url_hash?: string;
  draft_index?: number;
  status?: FeedbackStatus;
  page?: number;
  page_size?: number;
}

export interface FeedbackListResponse {
  items: Feedback[];
  total: number;
  avg_rating: number;
  page: number;
  page_size: number;
}

export interface FeedbackStats {
  groups: Array<{ key: string; count: number; avg_rating: number }>;
  total: number;
  overall_avg: number;
}

export interface FeedbackCreateResponse {
  feedback_id: string;
  created_at: string;
}

export interface FeedbackUpdateResponse {
  feedback_id: string;
  updated: boolean;
  updated_at: string;
}

export interface FeedbackDeleteResponse {
  feedback_id: string;
  deleted: boolean;
}

// ═══════════════════════════════════════════════════════════
// Activity（用户操作记录）
// ═══════════════════════════════════════════════════════════

export type ActionType =
  | 'draft_view'
  | 'draft_download'
  | 'draft_revise'
  | 'revision_apply'
  | 'feedback_submit'
  | 'pipeline_run';

export interface ActivityTarget {
  article_url_hash?: string;
  draft_index?: number;
  template?: string;
  template_id?: string;
  template_key?: string;
  template_version?: number;
  template_name?: string;
  perspective?: string;
  revision_id?: string;
  pipeline_id?: string;
}

export interface UserActivity {
  activity_id: string;
  user_id: string;
  action: ActionType;
  target: ActivityTarget;
  context: Record<string, unknown>;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface UserActivityCreate {
  action: ActionType;
  target: ActivityTarget;
  context?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
}

export interface ActivityQuery {
  action?: ActionType;
  article_url_hash?: string;
  page?: number;
  page_size?: number;
}

export interface ActivityListResponse {
  items: UserActivity[];
  total: number;
  page: number;
  page_size: number;
}

export interface ActivityStats {
  total: number;
  by_action: Partial<Record<ActionType, number>>;
  by_template: Record<string, number>;
  daily_trend: Array<{ date: string; count: number }>;
}

export interface ActivityLogResponse {
  activity_id: string;
  created_at: string;
}

export interface ActivityBatchLogResponse {
  activity_ids: string[];
  recorded: number;
  failed: number;
}

// ═══════════════════════════════════════════════════════════
// Style Profile（用户风格画像）
// ═══════════════════════════════════════════════════════════

export type PreferredLength = 'short' | 'medium' | 'long';

export type PreferredTone = 'market_oriented' | 'technical' | 'executive';

export interface StyleHints {
  preferred_templates: string[];
  preferred_perspectives: string[];
  preferred_length: PreferredLength;
  preferred_tone: PreferredTone;
  common_revise_directions: string[];
  avoid_patterns: string[];
}

export interface PreferenceMetric {
  count: number;
  avg_rating: number;
  download_count: number;
  apply_count: number;
  revise_count: number;
  template_id?: string | null;
  template_key?: string | null;
  display_name?: string | null;
  historical_names?: string[];
  legacy?: boolean;
}

export interface PreferenceScores {
  template_scores: Record<string, PreferenceMetric>;
  perspective_scores: Record<string, PreferenceMetric>;
}

export interface ProfileFeedbackSummary {
  total_feedbacks: number;
  avg_rating: number;
  positive_count: number;
  negative_count: number;
  neutral_count: number;
  top_tags: string[];
}

export interface ProfileActivitySummary {
  total_downloads: number;
  total_applies: number;
  total_revises: number;
  total_feedbacks: number;
  last_active_at?: string | null;
}

export interface ReviseInstructionPattern {
  pattern: string;
  count: number;
}

export interface StyleProfile {
  user_id: string;
  style_hints: StyleHints;
  preference_scores: PreferenceScores;
  feedback_summary: ProfileFeedbackSummary;
  activity_summary: ProfileActivitySummary;
  revise_instruction_patterns: ReviseInstructionPattern[];
  llm_analysis: string;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface ProfileRebuildResponse {
  rebuilt: boolean;
  feedback_count: number;
  activity_count: number;
  version: number;
  updated_at: string;
}

// ────────────────────────────────────────────────────────────
// PR Templates（多租户用户模板）
// ────────────────────────────────────────────────────────────
export type PRTemplateKey = 'breaking_a' | 'breaking_b' | 'law_a' | 'law_b' | 'ai_a' | 'ai_b';

export type PRTemplateCategory = '爆点事件' | '法律法规/监管动态' | 'AI技术重大进展';

export type PRTemplateSource = 'system' | 'user';

export interface PRTemplateSection {
  heading: string;
  guide: string;
  order: number;
}

export interface PRTemplateContent {
  name: string;
  title_template: string;
  sections: PRTemplateSection[];
  perspectives: [string, string];
  extra_instructions: string;
}

export interface EffectivePRTemplate extends PRTemplateContent {
  template_id: string;
  template_key: PRTemplateKey;
  category_v2: PRTemplateCategory;
  slot: 'A' | 'B';
  source: PRTemplateSource;
  version: number;
  system_version: number;
  updated_at?: string | null;
}

export interface PRTemplateUpdate extends PRTemplateContent {
  expected_version?: number;
}

export interface PRTemplateSnapshot extends PRTemplateContent {
  template_key: PRTemplateKey;
  category_v2: PRTemplateCategory;
  slot: 'A' | 'B';
}

export type PRTemplateChangeType = 'create' | 'update' | 'reset' | 'restore';

export interface PRTemplateVersion {
  version_id: string;
  template_id: string;
  template_key: PRTemplateKey;
  version: number;
  snapshot: PRTemplateSnapshot;
  change_type: PRTemplateChangeType;
  created_at: string;
}

export interface PRTemplateListResponse {
  items: EffectivePRTemplate[];
  total: number;
}

export interface PRTemplateVersionListResponse {
  items: PRTemplateVersion[];
  total: number;
  page: number;
  page_size: number;
}
