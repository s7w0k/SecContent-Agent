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
  ApplyRevisionResponse,
  Article,
  ArticleQuery,
  AuthResponse,
  ChatAskRequest,
  ChatAskResponse,
  ChatMessage,
  DevLogQuery,
  DevLogQueryResult,
  DevLogStats,
  DevLogTrace,
  DraftReview,
  DraftReviseRequest,
  DraftReviseResponse,
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
  HotArticle,
  HotRankingQuery,
  KnowledgeSummary,
  LoginRequest,
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
  PollLoginResult,
  ProfileRebuildResponse,
  QRCodeResult,
  RegisterRequest,
  Report,
  StatsData,
  StyleProfile,
  UploadArticleResult,
  User,
  UserActivityCreate,
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
  async scoreV2Single(urlHash: string): Promise<{
    ok: boolean;
    product_relevance: number;
    event_impact: number;
    pr_total_score: number;
    is_pr_candidate: boolean;
  }> {
    const { data } = await client.post(`/pipeline/score-v2/${urlHash}`);
    return data;
  },

  /** V2 智能 PR 流水线（单文章） */
  async runV2Single(urlHash: string): Promise<PipelineTaskResponse> {
    const { data } = await client.post(`/pipeline/run-v2/${urlHash}`);
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
  async refreshArticles(): Promise<{ ok: boolean; saved: number; total: number }> {
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
// Chat API（对话改稿）
// ═══════════════════════════════════════════════════════════

export const chatApi = {
  /** 对话问答 */
  async ask(request: ChatAskRequest): Promise<ChatAskResponse> {
    const { data } = await client.post('/chat/ask', request);
    return data.data;
  },

  /**
   * 流式对话问答（SSE）
   *
   * 通过 fetch + ReadableStream 接收 SSE 事件，逐 chunk 回调。
   *
   * @param request 问答请求
   * @param onChunk  每收到一个文本片段时的回调
   * @param onDone   流结束时的回调（收到完整回答）
   * @param onError  出错时的回调
   */
  async askStream(
    request: ChatAskRequest,
    onChunk: (chunk: string) => void,
    onDone?: (fullAnswer: string) => void,
    onError?: (error: string) => void,
  ): Promise<void> {
    const url = buildSSEUrl('/chat/ask_stream');

    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
      });

      if (!response.ok) {
        const errorText = await response.text();
        onError?.(errorText || `HTTP ${response.status}`);
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) {
        onError?.('无法读取响应流');
        return;
      }

      const decoder = new TextDecoder();
      let buffer = '';

      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // 按 SSE 事件分割（双换行）
        const lines = buffer.split('\n\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const jsonStr = line.slice(6).trim();
          if (!jsonStr) continue;

          try {
            const event = JSON.parse(jsonStr);
            if (event.chunk) {
              onChunk(event.chunk);
            } else if (event.done && event.answer !== undefined) {
              onDone?.(event.answer);
              return;
            } else if (event.error) {
              onError?.(event.error);
              return;
            }
          } catch {
            // JSON 解析失败，跳过
          }
        }
      }
    } catch (err) {
      onError?.(err instanceof Error ? err.message : '网络错误');
    }
  },

  /** 生成修订稿 */
  async reviseDraft(
    urlHash: string,
    draftIndex: number,
    request: DraftReviseRequest,
  ): Promise<DraftReviseResponse> {
    const { data } = await client.post(`/articles/${urlHash}/drafts/${draftIndex}/revise`, request);
    return data.data;
  },

  /**
   * 流式改稿（SSE）
   *
   * @param onChunk     每收到一个文本片段时的回调
   * @param onDone      流结束时的回调（收到解析后的修订稿数据）
   * @param onError     出错时的回调
   */
  async reviseDraftStream(
    urlHash: string,
    draftIndex: number,
    request: DraftReviseRequest,
    onChunk: (chunk: string) => void,
    onDone?: (result: DraftReviseResponse) => void,
    onError?: (error: string) => void,
  ): Promise<void> {
    const url = buildSSEUrl(`/articles/${urlHash}/drafts/${draftIndex}/revise_stream`);

    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
      });

      if (!response.ok) {
        const errorText = await response.text();
        onError?.(errorText || `HTTP ${response.status}`);
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) {
        onError?.('无法读取响应流');
        return;
      }

      const decoder = new TextDecoder();
      let buffer = '';

      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split('\n\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const jsonStr = line.slice(6).trim();
          if (!jsonStr) continue;

          try {
            const event = JSON.parse(jsonStr);
            if (event.chunk) {
              onChunk(event.chunk);
            } else if (event.done) {
              onDone?.({
                revision_id: event.revision_id,
                revised_content_md: event.revised_content_md,
                change_summary: event.change_summary || [],
                saved: event.saved || false,
              });
              return;
            } else if (event.error) {
              onError?.(event.error);
              return;
            }
          } catch {
            // JSON 解析失败，跳过
          }
        }
      }
    } catch (err) {
      onError?.(err instanceof Error ? err.message : '网络错误');
    }
  },

  /** 应用修订稿 */
  async applyRevision(
    urlHash: string,
    draftIndex: number,
    revisionId: string,
  ): Promise<ApplyRevisionResponse> {
    const { data } = await client.post(
      `/articles/${urlHash}/drafts/${draftIndex}/revisions/${revisionId}/apply`,
    );
    return data.data;
  },

  /** 重新检查稿件内容与宣传话术 */
  async reviewDraft(urlHash: string, draftIndex: number): Promise<DraftReview> {
    const { data } = await client.post(`/articles/${urlHash}/drafts/${draftIndex}/review`);
    return data.data;
  },

  /** 获取对话历史 */
  async getChatHistory(urlHash: string, draftIndex: number): Promise<ChatMessage[]> {
    const { data } = await client.get(`/articles/${urlHash}/drafts/${draftIndex}/chat-history`);
    return data.data.messages;
  },

  /** 清空对话历史 */
  async clearChatHistory(urlHash: string, draftIndex: number): Promise<{ cleared: boolean }> {
    const { data } = await client.delete(`/articles/${urlHash}/drafts/${draftIndex}/chat-history`);
    return data.data;
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
};

// ═══════════════════════════════════════════════════════════
// 统一导出
// ═══════════════════════════════════════════════════════════

const api = {
  ...authApi,
  ...dashboardApi,
  ...pipelineApi,
  ...reportsApi,
  ...accountsApi,
  ...chatApi,
  ...feedbackApi,
  ...activityApi,
  ...profileApi,
  ...logsApi,
  devLogs: devLogsApi,
  prTemplates: prTemplateApi,
  prompts: promptApi,
};

export default api;
