/**
 * API Client — Axios 封装
 *
 * 统一管理所有后端 API 调用，提供类型安全的返回值和错误处理。
 *
 * 使用:
 *   import api from './api/client';
 *   const stats = await api.getStats();
 *   const articles = await api.getArticles({ page: 1 });
 */

import axios, { type AxiosInstance } from 'axios';
import type {
  AccountStatusResult,
  ActivityBatchLogResponse,
  ActivityListResponse,
  ActivityLogResponse,
  ActivityQuery,
  ActivityStats,
  Article,
  ArticleQuery,
  AuthResponse,
  CreateAutonomousRunRequest,
  DevLogQuery,
  DevLogQueryResult,
  DevLogStats,
  DevLogTrace,
  EffectivePRTemplate,
  EffectivePrompt,
  FeedbackCreate,
  FeedbackCreateResponse,
  FeedbackDeleteResponse,
  FeedbackListResponse,
  FeedbackQuery,
  FeedbackStats,
  FeedbackUpdate,
  FeedbackUpdateResponse,
  GenerationPreferences,
  HotArticle,
  HotRankingQuery,
  KnowledgeDocument,
  KnowledgeDraft,
  KnowledgePreviewArticle,
  KnowledgePromptPreview,
  KnowledgeScorePreview,
  KnowledgeSearchResult,
  KnowledgeStatus,
  KnowledgeSummary,
  KnowledgeTree,
  KnowledgeUsageItem,
  KnowledgeValidationResult,
  LoginRequest,
  MemoryItem,
  MemoryListResponse,
  PRTemplateCategory,
  PRTemplateKey,
  PRTemplateListResponse,
  PRTemplateUpdate,
  PRTemplateVersionListResponse,
  PaginatedResponse,
  PipelineLogsResponse,
  PipelineResult,
  PipelineStatusResponse,
  PipelineTask,
  PipelineTaskList,
  PipelineTaskResponse,
  PolicyUpdateRequest,
  PollLoginResult,
  ProductCatalogItem,
  ProductScore,
  ProfilePolicy,
  ProfileRebuildResponse,
  PromptCatalogItem,
  PromptDetail,
  PromptValidationResult,
  PromptVersion,
  QRCodeResult,
  RegisterRequest,
  Report,
  RuntimeEventEnvelope,
  RuntimeSummary,
  SearchImportResponse,
  SearchStatusResponse,
  StatsData,
  StyleProfile,
  UploadArticleResult,
  User,
  UserActivityCreate,
  UserKnowledgeEntry,
  UserProduct,
  UserProductListItem,
  WebSearchResponse,
} from '../types';

// ═══════════════════════════════════════════════════════════
// Axios 实例
// ═══════════════════════════════════════════════════════════

const BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api';

export const ACCESS_TOKEN_KEY = 'access_token';
export const AUTH_UNAUTHORIZED_EVENT = 'auth:unauthorized';

export function getAccessToken(): string | null {
  return typeof window === 'undefined' ? null : window.localStorage.getItem(ACCESS_TOKEN_KEY);
}

export function buildSSEUrl(
  path: string,
  params: Record<string, string | number | boolean | null | undefined> = {},
): string {
  const basePath = `${BASE_URL.replace(/\/$/, '')}/${path.replace(/^\//, '')}`;
  const origin = typeof window === 'undefined' ? 'http://localhost' : window.location.origin;
  const url = new URL(basePath, origin);
  const token = getAccessToken();
  if (token) url.searchParams.set('token', token);
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined) url.searchParams.set(key, String(value));
  }
  return url.toString();
}

const client: AxiosInstance = axios.create({
  baseURL: BASE_URL,
  timeout: 300000,
  headers: {
    'Content-Type': 'application/json',
  },
});

/** 上传本地文章并返回入库结果。 */
export async function uploadArticle(file: File, title?: string): Promise<UploadArticleResult> {
  const form = new FormData();
  form.append('file', file);
  if (title?.trim()) form.append('title', title.trim());
  const { data } = await client.post('/upload/article', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return data.data;
}

// 请求拦截器：自动携带 JWT
client.interceptors.request.use((config) => {
  const token = getAccessToken();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// 响应拦截器：统一提取 data + 记录日志
client.interceptors.response.use(
  (response) => {
    return response;
  },
  (error) => {
    if (error.response?.status === 401 && typeof window !== 'undefined') {
      window.localStorage.removeItem(ACCESS_TOKEN_KEY);
      window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT));
    }
    const message = error.response?.data?.detail || error.message || 'Network error';
    console.error(`[API] ERROR ${error.response?.status || ''} ${error.config?.url}: ${message}`);
    return Promise.reject(error);
  },
);

// ═══════════════════════════════════════════════════════════
// Authentication API
// ═══════════════════════════════════════════════════════════

export const authApi = {
  async register(payload: RegisterRequest): Promise<User> {
    const { data } = await client.post('/auth/register', payload);
    return data.data;
  },

  async login(payload: LoginRequest): Promise<AuthResponse> {
    const { data } = await client.post('/auth/login', payload);
    return data.data;
  },

  async me(): Promise<User> {
    const { data } = await client.get('/auth/me');
    return data.data;
  },

  async deleteAccount(password?: string): Promise<{ message: string }> {
    const { data } = await client.delete('/auth/account', {
      data: password ? { password } : undefined,
    });
    return data.data;
  },
};

// ═══════════════════════════════════════════════════════════
// Dashboard API
// ═══════════════════════════════════════════════════════════

export const dashboardApi = {
  uploadArticle,

  /** 文章列表（分页+筛选+排序） */
  async getArticles(query: ArticleQuery = {}): Promise<PaginatedResponse<Article>> {
    const { data } = await client.get('/articles', { params: query });
    return data;
  },

  /** 单篇文章详情 */
  async getArticle(hash: string): Promise<Article> {
    const { data } = await client.get(`/articles/${hash}`);
    return data;
  },

  /** 删除文章 */
  async deleteArticle(hash: string): Promise<{ ok: boolean }> {
    const { data } = await client.delete(`/articles/${hash}`);
    return data;
  },

  /** 批量删除文章 */
  async batchDeleteArticles(hashes: string[]): Promise<{ ok: boolean; deleted: number }> {
    const { data } = await client.delete('/articles/batch', { data: { url_hashes: hashes } });
    return data;
  },

  /** 删除所有不相关文章 */
  async deleteIrrelevantArticles(): Promise<{ ok: boolean; deleted: number }> {
    const { data } = await client.delete('/articles/irrelevant');
    return data;
  },

  /** 获取推文原文 */
  async fetchContent(hash: string): Promise<{ ok: boolean; content: string }> {
    const { data } = await client.post(`/articles/${hash}/fetch-content`);
    return data;
  },

  /** 批量补抓原文 */
  async batchFetchContent(): Promise<{
    ok: boolean;
    data: { total: number; updated: number; message?: string };
  }> {
    const { data } = await client.post('/articles/batch-fetch-content');
    return data;
  },

  /** 生成摘要 */
  async summarizeArticle(hash: string): Promise<{ ok: boolean; summary_cn: string }> {
    const { data } = await client.post(`/articles/${hash}/summarize`);
    return data;
  },

  /** 统计概览 */
  async getStats(): Promise<StatsData> {
    const { data } = await client.get('/stats');
    return data;
  },

  /** 热点文章排行 */
  async getHotRanking(query: HotRankingQuery = {}): Promise<HotArticle[]> {
    const { limit = 10, category = 'all', date_range = '7d' } = query;
    const { data } = await client.get('/articles/hot', {
      params: { limit, category, date_range },
    });
    return data.data.items;
  },
};

// ═══════════════════════════════════════════════════════════
// Profile Policy API（用户显式偏好策略）
// ═══════════════════════════════════════════════════════════

export const policyApi = {
  async getPolicy(): Promise<{
    ok: boolean;
    data: { policy: ProfilePolicy; is_default: boolean; version: number };
  }> {
    const { data } = await client.get('/profile-policy');
    return data;
  },

  async savePolicy(
    policy: PolicyUpdateRequest,
    version: number,
  ): Promise<{ ok: boolean; data: { policy: ProfilePolicy; version: number } }> {
    const { data } = await client.put('/profile-policy', policy, {
      headers: { 'If-Match': String(version) },
    });
    return data;
  },

  async resetPolicy(): Promise<{
    ok: boolean;
    data: { policy: ProfilePolicy; is_default: boolean; version: number };
  }> {
    const { data } = await client.post('/profile-policy/reset');
    return data;
  },
};

// ═══════════════════════════════════════════════════════════
// Pipeline API
// ═══════════════════════════════════════════════════════════

export const pipelineApi = {
  /** 触发完整流水线 */
  async run(crawlDays = 1): Promise<PipelineResult> {
    const { data } = await client.post('/pipeline/run', {
      crawl_days: crawlDays,
    });
    return data;
  },

  /** 爬取+分类 */
  async crawl(crawlDays = 1): Promise<PipelineTaskResponse> {
    const { data } = await client.post('/pipeline/crawl', { crawl_days: crawlDays });
    return data;
  },

  /** 仅爬取海外新闻 */
  async crawlOverseas(days = 1): Promise<{
    ok: boolean;
    saved: number;
    total?: number;
    per_site?: Record<string, number>;
    errors?: Record<string, string>;
  }> {
    const { data } = await client.post('/pipeline/crawl-overseas', null, { params: { days } });
    return data;
  },

  /** 仅爬取公众号 */
  async crawlWewe(): Promise<{ ok: boolean; saved: number }> {
    const { data } = await client.post('/pipeline/crawl-wewe');
    return data;
  },

  /** 仅打分 */
  async score(): Promise<PipelineResult> {
    const { data } = await client.post('/pipeline/score', {});
    return data;
  },

  /** 仅生成报道 */
  async report(): Promise<PipelineResult> {
    const { data } = await client.post('/pipeline/report', {});
    return data;
  },

  /** 查询流水线状态 */
  async getStatus(): Promise<PipelineStatusResponse> {
    const { data } = await client.get('/pipeline/status');
    return data;
  },

  /** 导入 WeWe RSS 全部文章 */
  async importWewe(): Promise<{ ok: boolean; saved: number; total_in_rss: number }> {
    const { data } = await client.post('/pipeline/import-wewe');
    return data;
  },

  /** V2 双维度打分（批量） */
  async scoreV2(): Promise<{ ok: boolean; total: number; scored: number; candidates: number }> {
    const { data } = await client.post('/pipeline/score-v2');
    return data;
  },

  /** 创建 V2 批量打分后台任务 */
  async scoreV2Task(): Promise<PipelineTaskResponse> {
    const { data } = await client.post('/pipeline/score-v2/tasks');
    return data;
  },

  /** V2 双维度打分（单篇） */
  async scoreV2Single(
    urlHash: string,
    selectedProductIds?: string[],
  ): Promise<{
    ok: boolean;
    product_relevance: number;
    event_impact: number;
    pr_total_score: number;
    is_pr_candidate: boolean;
    product_scores: ProductScore[];
    skipped: boolean;
  }> {
    const { data } = await client.post(`/pipeline/score-v2/${urlHash}`, {
      selected_product_ids: selectedProductIds,
    });
    return data;
  },

  /** V2 智能 PR 流水线（单文章） */
  async runV2Single(
    urlHash: string,
    referenceTemplate?: string,
    options?: {
      product_target_mode?: string;
      selected_product_ids?: string[];
      product_relevance_enabled?: boolean;
      force_generate?: boolean;
      draft_variants?: 1 | 2 | 4;
    },
  ): Promise<PipelineTaskResponse> {
    const body: Record<string, unknown> = {};
    if (referenceTemplate) body.reference_template = referenceTemplate;
    if (options) Object.assign(body, options);
    const { data } = await client.post(`/pipeline/run-v2/${urlHash}`, body);
    return data;
  },

  /** V2 智能 PR 流水线（全量） */
  async runV2(crawlDays = 1): Promise<PipelineTaskResponse> {
    const { data } = await client.post('/pipeline/run-v2', { crawl_days: crawlDays });
    return data;
  },

  /** V2 流水线状态 */
  async getStatusV2(): Promise<PipelineStatusResponse> {
    const { data } = await client.get('/pipeline/status-v2');
    return data;
  },

  /** 查询异步流水线任务状态 */
  async getTaskStatus(taskId: string): Promise<PipelineTask> {
    const { data } = await client.get(`/pipeline/tasks/${taskId}`);
    return data.data;
  },

  /** 查询当前用户异步任务列表 */
  async getTasks(page = 1, pageSize = 20): Promise<PipelineTaskList> {
    const { data } = await client.get('/pipeline/tasks', {
      params: { page, page_size: pageSize },
    });
    return data.data;
  },

  /** V2 6分类 */
  async classifyV2(
    urlHashes?: string[],
    force = false,
  ): Promise<{
    ok: boolean;
    total: number;
    classified: number;
    summary: Record<string, number>;
    results: Array<{
      category_v2: string;
      category_v2_confidence: number;
      category_v2_reason: string;
      category_v2_fallback: boolean;
      is_ai_agent_security_relevant: boolean;
      ai_agent_security_relevance_confidence: number;
      ai_agent_security_relevance_reason: string;
      is_pr_eligible: boolean;
    }>;
  }> {
    const { data } = await client.post('/pipeline/classify-v2', {
      url_hashes: urlHashes || null,
      force,
    });
    return data;
  },

  /** 创建 V2 批量分类后台任务 */
  async classifyV2Task(): Promise<PipelineTaskResponse> {
    const { data } = await client.post('/pipeline/classify-v2/tasks');
    return data;
  },

  /** 读取 MultiAgent run 的计划与步骤账本（前端树形视图） */
  async getRunPlan(runId: string): Promise<{
    ok: boolean;
    data: {
      run_id: string;
      plan: Record<string, unknown> | null;
      steps: Array<Record<string, unknown>>;
    };
  }> {
    const { data } = await client.get(`/pipeline/runs/${runId}/plan`);
    return data;
  },

  /** 读取 MultiAgent run 的步骤账本 */
  async getRunSteps(runId: string): Promise<{
    ok: boolean;
    data: { run_id: string; steps: Array<Record<string, unknown>> };
  }> {
    const { data } = await client.get(`/pipeline/runs/${runId}/steps`);
    return data;
  },

  /** 读取 MultiAgent run 的观测事件流（脱敏） */
  async getRunEvents(
    runId: string,
    options: { eventType?: string; limit?: number } = {},
  ): Promise<{
    ok: boolean;
    data: {
      run_id: string;
      events: Array<{
        event_type: string;
        run_id: string;
        plan_id: string;
        step_id: string;
        worker: string;
        version: string;
        attempt: number;
        sequence: number;
        input_hash: string;
        result_hash: string;
        queue_ms: number;
        duration_ms: number;
        error_type: string | null;
        status: string;
        created_at: string;
      }>;
    };
  }> {
    const { data } = await client.get(`/pipeline/runs/${runId}/events`, {
      params: {
        event_type: options.eventType || undefined,
        limit: options.limit || 500,
      },
    });
    return data;
  },
};

export const logsApi = {
  async getDates(): Promise<string[]> {
    const { data } = await client.get('/logs/dates');
    return data.dates || [];
  },

  async getByDate(date: string): Promise<PipelineLogsResponse> {
    const { data } = await client.get(`/logs/${date}`);
    return { logs: data.logs || [], phases: data.phases || [] };
  },
};

export const devLogsApi = {
  async query(query: DevLogQuery = {}): Promise<DevLogQueryResult> {
    const params = {
      ...query,
      phase: query.phase?.join(','),
      level: query.level?.join(','),
    };
    const { data } = await client.get('/dev/logs', { params });
    return data.data;
  },

  async dates(): Promise<string[]> {
    const { data } = await client.get('/dev/logs/dates');
    return data.data.dates || [];
  },

  async trace(traceId: string): Promise<DevLogTrace> {
    const { data } = await client.get(`/dev/logs/trace/${encodeURIComponent(traceId)}`);
    return data.data;
  },

  async stats(date?: string): Promise<DevLogStats> {
    const { data } = await client.get('/dev/logs/stats', { params: { date } });
    return data.data;
  },
};

// ═══════════════════════════════════════════════════════════
// Reports API
// ═══════════════════════════════════════════════════════════

export const reportsApi = {
  /** 报道列表 */
  async getReports(page = 1, pageSize = 10): Promise<PaginatedResponse<Report>> {
    const { data } = await client.get('/reports', {
      params: { page, page_size: pageSize },
    });
    return data;
  },

  /** 报道详情 */
  async getReport(id: string): Promise<Report> {
    const { data } = await client.get(`/reports/${id}`);
    return data;
  },

  /** 知识库摘要 */
  async getKnowledge(): Promise<KnowledgeSummary> {
    const { data } = await client.get('/knowledge');
    return data;
  },
};

// ═══════════════════════════════════════════════════════════
// Accounts API（公众号账号管理）
// ═══════════════════════════════════════════════════════════

export const accountsApi = {
  /** 查询所有账号状态 */
  async getAccountStatus(): Promise<AccountStatusResult> {
    const { data } = await client.get('/accounts/status');
    return data;
  },

  /** 创建登录二维码 */
  async createQRCode(): Promise<QRCodeResult> {
    const { data } = await client.post('/accounts/qrcode');
    return data;
  },

  /** 轮询扫码结果 */
  async pollLogin(uuid: string, timeout = 120): Promise<PollLoginResult> {
    const { data } = await client.post('/accounts/poll-login', null, {
      params: { uuid, timeout_seconds: timeout },
    });
    return data;
  },

  /** 保存账号到 WeWe RSS */
  async saveAccount(vid: string, token: string, name: string): Promise<{ ok: boolean }> {
    const { data } = await client.post('/accounts/save', null, {
      params: { vid, token, name },
    });
    return data;
  },

  /** 启用/停用账号 */
  async toggleAccount(accountId: string, status: number): Promise<{ ok: boolean }> {
    const { data } = await client.post('/accounts/toggle', null, {
      params: { account_id: accountId, status },
    });
    return data;
  },

  /** 更新全部最新文章 */
  async refreshArticles(): Promise<{
    ok: boolean;
    message?: string;
    saved?: number;
    total?: number;
  }> {
    const { data } = await client.post('/accounts/refresh');
    return data;
  },

  /** 删除账号 */
  async deleteAccount(accountId: string): Promise<{ ok: boolean }> {
    const { data } = await client.delete(`/accounts/delete/${accountId}`);
    return data;
  },
};

// ═══════════════════════════════════════════════════════════
// Feedback API（用户反馈）
// ═══════════════════════════════════════════════════════════

export const feedbackApi = {
  /** 提交反馈 */
  async create(payload: FeedbackCreate): Promise<FeedbackCreateResponse> {
    const { data } = await client.post('/feedback', payload);
    return data.data;
  },

  /** 查询反馈 */
  async list(query: FeedbackQuery = {}): Promise<FeedbackListResponse> {
    const { data } = await client.get('/feedback', { params: query });
    return data.data;
  },

  /** 反馈统计 */
  async stats(groupBy: 'template' | 'perspective' = 'template'): Promise<FeedbackStats> {
    const { data } = await client.get('/feedback/stats', {
      params: { group_by: groupBy },
    });
    return data.data;
  },

  /** 更新反馈 */
  async update(feedbackId: string, payload: FeedbackUpdate): Promise<FeedbackUpdateResponse> {
    const { data } = await client.put(`/feedback/${feedbackId}`, payload);
    return data.data;
  },

  /** 删除反馈 */
  async remove(feedbackId: string): Promise<FeedbackDeleteResponse> {
    const { data } = await client.delete(`/feedback/${feedbackId}`);
    return data.data;
  },
};

// ═══════════════════════════════════════════════════════════
// Activity API（用户操作记录）
// ═══════════════════════════════════════════════════════════

export const activityApi = {
  /** 记录单条操作 */
  async log(payload: UserActivityCreate): Promise<ActivityLogResponse> {
    const { data } = await client.post('/activities/log', payload);
    return data.data;
  },

  /** 批量记录操作 */
  async batchLog(activities: UserActivityCreate[]): Promise<ActivityBatchLogResponse> {
    const { data } = await client.post('/activities/batch-log', { activities });
    return data.data;
  },

  /** 查询操作记录 */
  async list(query: ActivityQuery = {}): Promise<ActivityListResponse> {
    const { data } = await client.get('/activities', { params: query });
    return data.data;
  },

  /** 操作统计 */
  async stats(days = 30): Promise<ActivityStats> {
    const { data } = await client.get('/activities/stats', { params: { days } });
    return data.data;
  },
};

// ═══════════════════════════════════════════════════════════
// Profile API（用户风格画像）
// ═══════════════════════════════════════════════════════════

export const profileApi = {
  /** 获取用户风格画像 */
  async getStyle(): Promise<StyleProfile> {
    const { data } = await client.get('/profile/style');
    return data.data;
  },

  /** 重建用户风格画像 */
  async rebuild(): Promise<ProfileRebuildResponse> {
    const { data } = await client.post('/profile/rebuild');
    return data.data;
  },
};

// ────────────────────────────────────────────────────────────
// PR Template API（当前用户隔离）
// ────────────────────────────────────────────────────────────
export const prTemplateApi = {
  async list(category?: PRTemplateCategory): Promise<PRTemplateListResponse> {
    const { data } = await client.get('/pr-templates', {
      params: category ? { category_v2: category } : undefined,
    });
    return data.data;
  },

  async get(templateKey: PRTemplateKey): Promise<EffectivePRTemplate> {
    const { data } = await client.get(`/pr-templates/${templateKey}`);
    return data.data;
  },

  async save(templateKey: PRTemplateKey, payload: PRTemplateUpdate): Promise<EffectivePRTemplate> {
    const { data } = await client.put(`/pr-templates/${templateKey}`, payload);
    return data.data;
  },

  async preview(templateKey: PRTemplateKey, payload: PRTemplateUpdate): Promise<string> {
    const { data } = await client.post(`/pr-templates/${templateKey}/preview`, payload);
    return data.data.content_md;
  },

  async reset(templateKey: PRTemplateKey): Promise<EffectivePRTemplate> {
    const { data } = await client.post(`/pr-templates/${templateKey}/reset`);
    return data.data;
  },

  async versions(
    templateKey: PRTemplateKey,
    page = 1,
    pageSize = 20,
  ): Promise<PRTemplateVersionListResponse> {
    const { data } = await client.get(`/pr-templates/${templateKey}/versions`, {
      params: { page, page_size: pageSize },
    });
    return data.data;
  },

  async restore(templateKey: PRTemplateKey, version: number): Promise<EffectivePRTemplate> {
    const { data } = await client.post(`/pr-templates/${templateKey}/versions/${version}/restore`);
    return data.data;
  },
};

// ────────────────────────────────────────────────────────────
// User Prompt API（当前用户隔离）
// ────────────────────────────────────────────────────────────
export const promptApi = {
  async getDraftPrompt(): Promise<EffectivePrompt> {
    const { data } = await client.get('/user-prompts/draft-system');
    return data.data;
  },

  async saveDraftPrompt(content: string): Promise<EffectivePrompt> {
    const { data } = await client.put('/user-prompts/draft-system', { content });
    return data.data;
  },

  async resetDraftPrompt(): Promise<EffectivePrompt> {
    const { data } = await client.post('/user-prompts/draft-system/reset');
    return data.data;
  },

  // ── 提示词中心（Prompt Catalog） ──
  async list(): Promise<PromptCatalogItem[]> {
    const { data } = await client.get('/user-prompts');
    return data.data.items;
  },

  async get(promptKey: string): Promise<PromptDetail> {
    const { data } = await client.get(`/user-prompts/${promptKey}`);
    return data.data;
  },

  async validate(promptKey: string, content: string): Promise<PromptValidationResult> {
    const { data } = await client.post(`/user-prompts/${promptKey}/validate`, { content });
    return data.data;
  },

  async save(promptKey: string, content: string, expectedVersion?: number): Promise<PromptDetail> {
    const { data } = await client.put(`/user-prompts/${promptKey}`, {
      content,
      expected_version: expectedVersion,
    });
    return data.data;
  },

  async reset(promptKey: string): Promise<PromptDetail> {
    const { data } = await client.post(`/user-prompts/${promptKey}/reset`);
    return data.data;
  },

  async listVersions(
    promptKey: string,
    page = 1,
    pageSize = 30,
  ): Promise<{ items: PromptVersion[]; total: number; page: number; page_size: number }> {
    const { data } = await client.get(`/user-prompts/${promptKey}/versions`, {
      params: { page, page_size: pageSize },
    });
    return data.data;
  },

  async restoreVersion(promptKey: string, version: number): Promise<PromptDetail> {
    const { data } = await client.post(`/user-prompts/${promptKey}/versions/${version}/restore`);
    return data.data;
  },

  async preview(promptKey: string): Promise<{
    composed_preview: string;
    source: string;
    version: number | null;
    layers: Record<string, string>;
  }> {
    const { data } = await client.post(`/user-prompts/${promptKey}/preview`);
    return data.data;
  },
};

// ────────────────────────────────────────────────────────────
// Product Catalog API（产品目录）
// ────────────────────────────────────────────────────────────
export const productCatalogApi = {
  async list(purpose?: string): Promise<{ items: ProductCatalogItem[]; knowledge_hash: string }> {
    const { data } = await client.get('/product-catalog', { params: purpose ? { purpose } : {} });
    return data.data;
  },
};

// ────────────────────────────────────────────────────────────
// Generation Preferences API（生成偏好）
// ────────────────────────────────────────────────────────────
export const generationPreferencesApi = {
  async get(): Promise<GenerationPreferences> {
    const { data } = await client.get('/generation-preferences');
    return data.data;
  },

  async save(body: {
    product_relevance_enabled: boolean;
    product_target_mode: string;
    selected_product_ids: string[];
    expected_version?: number;
  }): Promise<GenerationPreferences> {
    const { data } = await client.put('/generation-preferences', body);
    return data.data;
  },

  async reset(): Promise<GenerationPreferences> {
    const { data } = await client.post('/generation-preferences/reset');
    return data.data;
  },
};

// ────────────────────────────────────────────────────────────
// User Knowledge API（用户级产品知识库）
// ────────────────────────────────────────────────────────────
export const userKnowledgeApi = {
  // ── 产品 ──
  async listProducts(): Promise<UserProductListItem[]> {
    const { data } = await client.get('/user-knowledge/products');
    return data.data.items;
  },
  async createProduct(body: {
    name: string;
    description?: string;
    aliases?: string[];
    keywords?: string[];
    sort_order?: number;
    enabled?: boolean;
  }): Promise<UserProduct> {
    const { data } = await client.post('/user-knowledge/products', body);
    return data.data;
  },
  async updateProduct(
    productId: string,
    body: {
      name?: string;
      description?: string;
      aliases?: string[];
      keywords?: string[];
      sort_order?: number;
      enabled?: boolean;
    },
  ): Promise<UserProduct> {
    const { data } = await client.put(`/user-knowledge/products/${productId}`, body);
    return data.data;
  },
  async deleteProduct(productId: string): Promise<void> {
    await client.delete(`/user-knowledge/products/${productId}`);
  },
  async listEntriesByProduct(productId: string): Promise<UserKnowledgeEntry[]> {
    const { data } = await client.get(`/user-knowledge/products/${productId}`);
    return data.data.items;
  },

  // ── 知识条目 ──
  async listEntries(): Promise<UserKnowledgeEntry[]> {
    const { data } = await client.get('/user-knowledge');
    return data.data.items;
  },
  async createEntry(body: {
    product_id: string;
    product_scope: 'global' | 'user';
    doc_type: 'overview' | 'market-brief' | 'sales-brief' | 'custom';
    title: string;
    content: string;
    enabled?: boolean;
    sort_order?: number;
  }): Promise<UserKnowledgeEntry> {
    const { data } = await client.post('/user-knowledge', body);
    return data.data;
  },
  async getEntry(entryId: string): Promise<UserKnowledgeEntry> {
    const { data } = await client.get(`/user-knowledge/${entryId}`);
    return data.data;
  },
  async updateEntry(
    entryId: string,
    body: {
      product_id?: string;
      product_scope?: 'global' | 'user';
      doc_type?: 'overview' | 'market-brief' | 'sales-brief' | 'custom';
      title?: string;
      content?: string;
      enabled?: boolean;
      sort_order?: number;
    },
  ): Promise<UserKnowledgeEntry> {
    const { data } = await client.put(`/user-knowledge/${entryId}`, body);
    return data.data;
  },
  async deleteEntry(entryId: string): Promise<void> {
    await client.delete(`/user-knowledge/${entryId}`);
  },
  async toggleEntry(entryId: string): Promise<{ entry_id: string; enabled: boolean }> {
    const { data } = await client.post(`/user-knowledge/${entryId}/toggle`);
    return data.data;
  },
  async preview(body: { product_ids: string[]; purpose: string }): Promise<{
    user_content: string;
    user_file_count: number;
  }> {
    const { data } = await client.post('/user-knowledge/preview', body);
    return data.data;
  },
};

// ────────────────────────────────────────────────────────────
// Memory API（用户记忆）
// ────────────────────────────────────────────────────────────
export const memoryApi = {
  async listItems(params?: {
    status?: string;
    dimension?: string;
    category_v2?: string;
    stage?: string;
    page?: number;
    page_size?: number;
  }): Promise<{ ok: boolean; data: MemoryListResponse }> {
    const { data } = await client.get('/memory/items', { params });
    return data;
  },

  async getItem(memoryId: string): Promise<{ ok: boolean; data: MemoryItem }> {
    const { data } = await client.get(`/memory/items/${memoryId}`);
    return data;
  },

  async approveItem(memoryId: string): Promise<{ ok: boolean }> {
    const { data } = await client.post(`/memory/items/${memoryId}/approve`);
    return data;
  },

  async rejectItem(memoryId: string): Promise<{ ok: boolean }> {
    const { data } = await client.post(`/memory/items/${memoryId}/reject`);
    return data;
  },

  async suppressItem(memoryId: string): Promise<{ ok: boolean }> {
    const { data } = await client.post(`/memory/items/${memoryId}/suppress`);
    return data;
  },

  async activateItem(memoryId: string): Promise<{ ok: boolean }> {
    const { data } = await client.post(`/memory/items/${memoryId}/activate`);
    return data;
  },

  async editItem(
    memoryId: string,
    body: { display_text: string; polarity?: string },
  ): Promise<{ ok: boolean }> {
    const { data } = await client.put(`/memory/items/${memoryId}`, body);
    return data;
  },

  async deleteItem(memoryId: string): Promise<{ ok: boolean }> {
    const { data } = await client.delete(`/memory/items/${memoryId}`);
    return data;
  },

  async recompile(): Promise<{ ok: boolean }> {
    const { data } = await client.post('/memory/recompile');
    return data;
  },

  async previewPack(body: {
    category_v2?: string;
    template_id?: string;
    stage?: string;
  }): Promise<{ ok: boolean; data: unknown }> {
    const { data } = await client.post('/memory/preview', body);
    return data;
  },
};

// ═══════════════════════════════════════════════════════════
// Knowledge Catalog API（产品知识库目录）
// ═══════════════════════════════════════════════════════════

export const knowledgeApi = {
  /** 获取知识库目录树 */
  async getTree(includeEmpty = true, includeRaw = true): Promise<KnowledgeTree> {
    const { data } = await client.get('/knowledge/tree', {
      params: { include_empty: includeEmpty, include_raw: includeRaw },
    });
    return data.data;
  },

  /** 获取文档内容与元数据 */
  async getDocument(path: string): Promise<KnowledgeDocument> {
    const { data } = await client.get('/knowledge/documents', { params: { path } });
    return data.data;
  },

  /** 搜索知识库文件 */
  async search(
    q: string,
    role?: string,
    directScoringPrompt?: boolean,
  ): Promise<KnowledgeSearchResult[]> {
    const params: Record<string, unknown> = { q };
    if (role) params.role = role;
    if (directScoringPrompt !== undefined) params.direct_scoring_prompt = directScoringPrompt;
    const { data } = await client.get('/knowledge/search', { params });
    return data.data;
  },

  /** 获取知识库加载状态 */
  async getStatus(): Promise<KnowledgeStatus> {
    const { data } = await client.get('/knowledge/status');
    return data.data;
  },

  /** 获取用途分类说明 */
  async getUsageMap(): Promise<KnowledgeUsageItem[]> {
    const { data } = await client.get('/knowledge/usage-map');
    return data.data;
  },
};

// ═══════════════════════════════════════════════════════════
// Knowledge Admin API（产品知识库草稿管理，K.5）
// ═══════════════════════════════════════════════════════════

export const knowledgeAdminApi = {
  /** 创建草稿 */
  async createDraft(documentId: string, baseContentHash: string): Promise<KnowledgeDraft> {
    const { data } = await client.post('/admin/knowledge/drafts', {
      document_id: documentId,
      base_content_hash: baseContentHash,
    });
    return data.data;
  },

  /** 获取草稿详情（含正式内容） */
  async getDraft(
    draftId: string,
  ): Promise<{ draft: KnowledgeDraft; formal_content: string; formal_hash: string }> {
    const { data } = await client.get(`/admin/knowledge/drafts/${draftId}`);
    return data.data;
  },

  /** 保存草稿 */
  async updateDraft(
    draftId: string,
    contentMd: string,
    changeSummary?: string,
  ): Promise<KnowledgeDraft> {
    const { data } = await client.put(`/admin/knowledge/drafts/${draftId}`, {
      content_md: contentMd,
      change_summary: changeSummary,
    });
    return data.data;
  },

  /** 放弃草稿 */
  async deleteDraft(draftId: string): Promise<void> {
    await client.delete(`/admin/knowledge/drafts/${draftId}`);
  },

  /** 列出草稿 */
  async listDrafts(relativePath?: string, status?: string): Promise<KnowledgeDraft[]> {
    const params: Record<string, string> = {};
    if (relativePath) params.relative_path = relativePath;
    if (status) params.status = status;
    const { data } = await client.get('/admin/knowledge/drafts', { params });
    return data.data;
  },

  /** 校验草稿内容与加载器一致性 */
  async validateDraft(draftId: string): Promise<KnowledgeValidationResult> {
    const { data } = await client.post(`/admin/knowledge/drafts/${draftId}/validate`);
    return data.data;
  },

  /** 预览草稿对评分 Prompt 的影响 */
  async previewPrompt(draftId: string): Promise<KnowledgePromptPreview> {
    const { data } = await client.post(`/admin/knowledge/drafts/${draftId}/preview-prompt`);
    return data.data;
  },

  /** 使用测试文章试打分（新旧 Prompt 对比） */
  async previewScore(
    draftId: string,
    article: KnowledgePreviewArticle,
  ): Promise<KnowledgeScorePreview> {
    const { data } = await client.post(`/admin/knowledge/drafts/${draftId}/preview-score`, {
      article,
    });
    return data.data;
  },

  /** 发布草稿到正式知识库 */
  async publish(
    draftIds: string[],
    versionName?: string,
    releaseNotes?: string,
  ): Promise<Record<string, unknown>> {
    const { data } = await client.post('/admin/knowledge/publications', {
      draft_ids: draftIds,
      version_name: versionName,
      release_notes: releaseNotes,
    });
    return data.data;
  },

  /** 发布历史列表 */
  async listPublications(limit = 20): Promise<Record<string, unknown>[]> {
    const { data } = await client.get('/admin/knowledge/publications', { params: { limit } });
    return data.data;
  },

  /** 发布详情 */
  async getPublication(publicationId: string): Promise<Record<string, unknown>> {
    const { data } = await client.get(`/admin/knowledge/publications/${publicationId}`);
    return data.data;
  },

  /** 回滚发布 */
  async rollback(publicationId: string, confirmVersion?: string): Promise<Record<string, unknown>> {
    const { data } = await client.post(`/admin/knowledge/publications/${publicationId}/rollback`, {
      confirm_version: confirmVersion,
    });
    return data.data;
  },
};

// ═══════════════════════════════════════════════════════════
// Web Search API（SearXNG 网络搜索）
// ═══════════════════════════════════════════════════════════

export const webSearchApi = {
  /** 查询搜索功能状态 */
  async getStatus(): Promise<SearchStatusResponse> {
    const { data } = await client.get('/search/status');
    return data.data;
  },

  /** 执行搜索 */
  async search(params: {
    q: string;
    categories?: string[];
    language?: string;
    time_range?: string;
    safesearch?: number;
    pageno?: number;
  }): Promise<WebSearchResponse> {
    const { data } = await client.post('/search/query', params, { timeout: 20000 });
    return data.data;
  },

  /** 获取已有搜索会话 */
  async getSession(searchId: string): Promise<WebSearchResponse> {
    const { data } = await client.get(`/search/sessions/${searchId}`);
    return data.data;
  },

  /** 导入搜索结果为文章 */
  async importResults(
    searchId: string,
    resultIds: string[],
    idempotencyKey: string,
  ): Promise<SearchImportResponse> {
    const { data } = await client.post(
      '/search/import',
      { search_id: searchId, result_ids: resultIds },
      { headers: { 'Idempotency-Key': idempotencyKey } },
    );
    return data.data;
  },
};

// ────────────────────────────────────────────────────────────
// Autonomous API（自主模式，阶段四 4A）
// ────────────────────────────────────────────────────────────
export const autonomousApi = {
  /** 创建并启动自主运行 */
  async createRun(body: CreateAutonomousRunRequest): Promise<RuntimeSummary> {
    const { data } = await client.post('/autonomous/runs', body);
    return data;
  },

  /** 运行列表（当前用户） */
  async listRuns(status?: string, limit = 50): Promise<RuntimeSummary[]> {
    const { data } = await client.get('/autonomous/runs', {
      params: { status: status || undefined, limit },
    });
    return data;
  },

  /** 运行详情（脱敏） */
  async getRun(runId: string): Promise<RuntimeSummary> {
    const { data } = await client.get(`/autonomous/runs/${runId}`);
    return data;
  },

  /** 取消运行（安全点停止） */
  async cancelRun(runId: string): Promise<{ run_id: string; status: string }> {
    const { data } = await client.post(`/autonomous/runs/${runId}/cancel`);
    return data;
  },

  /** 恢复运行（审批后） */
  async resumeRun(runId: string): Promise<RuntimeSummary> {
    const { data } = await client.post(`/autonomous/runs/${runId}/resume`);
    return data;
  },

  /** 回答结构化追问并从 checkpoint 继续 */
  async respond(
    runId: string,
    slotValues: Record<string, unknown> = {},
    turnId?: string,
    message?: string,
  ): Promise<RuntimeSummary> {
    const { data } = await client.post(`/autonomous/runs/${runId}/respond`, {
      slot_values: slotValues,
      turn_id: turnId || undefined,
      message: message || undefined,
    });
    return data;
  },

  /** 审批通过 */
  async approveApproval(
    approvalId: string,
  ): Promise<{ approval_id: string; status: string; run_id: string }> {
    const { data } = await client.post(`/autonomous/approvals/${approvalId}/approve`);
    return data;
  },

  /** 审批拒绝 */
  async rejectApproval(
    approvalId: string,
  ): Promise<{ approval_id: string; status: string; run_id: string }> {
    const { data } = await client.post(`/autonomous/approvals/${approvalId}/reject`);
    return data;
  },

  /**
   * 运行事件流 SSE 地址（Last-Event-ID 断线续传由 EventSource 自动携带）
   *
   * 后端发送命名事件（event: {event_type}），必须按类型订阅；
   * 返回 EventSource 实例；调用方负责在组件卸载时关闭。
   * 事件载荷为 RuntimeEventEnvelope（脱敏）。
   */
  openEventSource(runId: string, onEvent: (event: RuntimeEventEnvelope) => void): EventSource {
    const url = buildSSEUrl(`/autonomous/runs/${runId}/events`);
    const source = new EventSource(url);
    const KNOWN_EVENT_TYPES = [
      'run_created',
      'step_planned',
      'policy_checked',
      'tool_executed',
      'step_failed',
      'waiting_approval',
      'approval_approved',
      'approval_rejected',
      'run_finished',
      'state_transition',
      'user_response_applied',
    ];
    const handleEvent = (msg: MessageEvent<string>) => {
      try {
        onEvent(JSON.parse(msg.data) as RuntimeEventEnvelope);
      } catch {
        // 解析失败跳过
      }
    };
    for (const type of KNOWN_EVENT_TYPES) {
      source.addEventListener(type, handleEvent);
    }
    // 服务端结束信号：关闭事件流（终态详情由轮询刷新）
    source.addEventListener('done', () => {
      source.close();
    });
    return source;
  },
};

// ────────────────────────────────────────────────────────────
// AgentEngine Chat API（真正的 LLM tool-loop + 聊天工作台）
// ────────────────────────────────────────────────────────────
export interface AgentEngineThinkingStep {
  type: 'text' | 'tool';
  text?: string;
  name?: string;
}
export interface AgentEngineMsg {
  role: string;
  content: string;
  draft?: { tool?: string; heading?: string; content?: string } | null;
  thinking?: AgentEngineThinkingStep[];
  created_at?: string;
}
export interface AgentEngineThread {
  thread_id: string;
  title: string;
  status: string;
  messages: AgentEngineMsg[];
  created_at: string;
  updated_at: string;
}
export interface AgentEngineEvent {
  sequence: number;
  event_type: string;
  run_id: string;
  thread_id: string;
  timestamp: string;
  data: Record<string, unknown>;
}

export const agentEngineApi = {
  async createThread(): Promise<AgentEngineThread> {
    const { data } = await client.post('/agent-engine/threads');
    return data;
  },
  async listThreads(limit = 50): Promise<AgentEngineThread[]> {
    const { data } = await client.get('/agent-engine/threads', { params: { limit } });
    return data;
  },
  async getThread(threadId: string): Promise<AgentEngineThread> {
    const { data } = await client.get(`/agent-engine/threads/${threadId}`);
    return data;
  },
  async sendMessage(
    threadId: string,
    content: string,
    manuscriptId?: string,
  ): Promise<AgentEngineThread> {
    const { data } = await client.post(`/agent-engine/threads/${threadId}/messages`, {
      content,
      manuscript_id: manuscriptId || null,
    });
    return data;
  },
  async resolveApproval(approvalId: string, approved: boolean): Promise<{ ok: boolean }> {
    const { data } = await client.post('/agent-engine/approvals/resolve', {
      approval_id: approvalId,
      approved,
    });
    return data;
  },
  async stopGeneration(threadId: string): Promise<{ ok: boolean; stopped: boolean }> {
    const { data } = await client.post(`/agent-engine/threads/${threadId}/stop`);
    return data;
  },
  openEventSource(
    threadId: string,
    onEvent: (event: AgentEngineEvent) => void,
    onDone?: () => void,
  ): EventSource {
    const source = new EventSource(buildSSEUrl(`/agent-engine/threads/${threadId}/events`));
    const KNOWN = [
      'run_started',
      'agent_message',
      'tool_call',
      'tool_result',
      'tool_error',
      'approval_requested',
      'approval_resolved',
      'final',
      'error',
      'user_message',
      'done',
      'interrupted',
      'resumed',
    ];
    const handler = (message: MessageEvent<string>) => {
      try {
        onEvent(JSON.parse(message.data) as AgentEngineEvent);
      } catch {
        // ignore malformed
      }
    };
    for (const type of KNOWN) source.addEventListener(type, handler);
    source.addEventListener('done', () => {
      source.close();
      onDone?.();
    });
    return source;
  },
};

// ═══════════════════════════════════════════════════════════
// 我的稿件库
// ═══════════════════════════════════════════════════════════
export interface Manuscript {
  manuscript_id: string;
  title: string;
  source: string;
  news_title?: string;
  content_length: number;
  created_at: string;
  updated_at: string;
}

export const manuscriptApi = {
  async create(input: {
    title: string;
    content_md: string;
    source?: string;
    news_title?: string;
  }): Promise<Manuscript> {
    const { data } = await client.post('/manuscripts/', input);
    return data.data as Manuscript;
  },
  async list(): Promise<Manuscript[]> {
    const { data } = await client.get('/manuscripts/');
    return (data.items as Manuscript[]) || [];
  },
  async get(manuscriptId: string): Promise<Manuscript & { content_md: string }> {
    const { data } = await client.get(`/manuscripts/${manuscriptId}`);
    return data.data as Manuscript & { content_md: string };
  },
  async download(manuscriptId: string): Promise<Blob> {
    const { data } = await client.get(`/manuscripts/${manuscriptId}/download`, {
      responseType: 'blob',
    });
    return data as Blob;
  },
  async remove(manuscriptId: string): Promise<void> {
    await client.delete(`/manuscripts/${manuscriptId}`);
  },
};

// ═══════════════════════════════════════════════════════════
// 统一导出
// ═══════════════════════════════════════════════════════════

const api = {
  ...authApi,
  ...dashboardApi,
  ...policyApi,
  ...pipelineApi,
  ...reportsApi,
  ...accountsApi,
  ...feedbackApi,
  ...activityApi,
  ...profileApi,
  ...logsApi,
  ...memoryApi,
  devLogs: devLogsApi,
  prTemplates: prTemplateApi,
  prompts: promptApi,
  productCatalog: productCatalogApi,
  generationPreferences: generationPreferencesApi,
  knowledge: knowledgeApi,
  knowledgeAdmin: knowledgeAdminApi,
  webSearchApi,
  autonomousApi,
  agentEngineApi,
  manuscriptApi,
};

export default api;
