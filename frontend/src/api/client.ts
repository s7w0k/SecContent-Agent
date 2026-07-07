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

import axios, { type AxiosInstance } from "axios";
import type {
  AccountStatusResult,
  ApplyRevisionResponse,
  Article,
  ArticleQuery,
  ChatAskRequest,
  ChatAskResponse,
  ChatMessage,
  DraftReviseRequest,
  DraftReviseResponse,
  KnowledgeSummary,
  PaginatedResponse,
  PipelineResult,
  PipelineStatusResponse,
  PollLoginResult,
  QRCodeResult,
  Report,
  StatsData,
} from "../types";

// ═══════════════════════════════════════════════════════════
// Axios 实例
// ═══════════════════════════════════════════════════════════

const BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api";

const client: AxiosInstance = axios.create({
  baseURL: BASE_URL,
  timeout: 300000,
  headers: {
    "Content-Type": "application/json",
  },
});

// 请求拦截器：记录日志
client.interceptors.request.use((config) => {
  console.log(`[API] ${config.method?.toUpperCase()} ${config.url}`, config.params || config.data || "");
  return config;
});

// 响应拦截器：统一提取 data + 记录日志
client.interceptors.response.use(
  (response) => {
    console.log(`[API] ${response.status} ${response.config.url} (${response.data?.total ?? "ok"})`);
    return response;
  },
  (error) => {
    const message = error.response?.data?.detail || error.message || "Network error";
    console.error(`[API] ERROR ${error.response?.status || ""} ${error.config?.url}: ${message}`);
    return Promise.reject(error);
  },
);

// ═══════════════════════════════════════════════════════════
// Dashboard API
// ═══════════════════════════════════════════════════════════

export const dashboardApi = {
  /** 文章列表（分页+筛选+排序） */
  async getArticles(
    query: ArticleQuery = {},
  ): Promise<PaginatedResponse<Article>> {
    const { data } = await client.get("/articles", { params: query });
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

  /** 获取推文原文 */
  async fetchContent(hash: string): Promise<{ ok: boolean; content: string }> {
    const { data } = await client.post(`/articles/${hash}/fetch-content`);
    return data;
  },

  /** 生成摘要 */
  async summarizeArticle(hash: string): Promise<{ ok: boolean; summary_cn: string }> {
    const { data } = await client.post(`/articles/${hash}/summarize`);
    return data;
  },

  /** 统计概览 */
  async getStats(): Promise<StatsData> {
    const { data } = await client.get("/stats");
    return data;
  },
};

// ═══════════════════════════════════════════════════════════
// Pipeline API
// ═══════════════════════════════════════════════════════════

export const pipelineApi = {
  /** 触发完整流水线 */
  async run(crawlDays: number = 1): Promise<PipelineResult> {
    const { data } = await client.post("/pipeline/run", {
      crawl_days: crawlDays,
    });
    return data;
  },

  /** 爬取+分类 */
  async crawl(crawlDays: number = 1): Promise<PipelineResult> {
    const { data } = await client.post("/pipeline/crawl", { crawl_days: crawlDays });
    return data;
  },

  /** 仅爬取海外新闻 */
  async crawlOverseas(days: number = 1): Promise<{ ok: boolean; saved: number }> {
    const { data } = await client.post("/pipeline/crawl-overseas", null, { params: { days } });
    return data;
  },

  /** 仅爬取公众号 */
  async crawlWewe(): Promise<{ ok: boolean; saved: number }> {
    const { data } = await client.post("/pipeline/crawl-wewe");
    return data;
  },

  /** 仅打分 */
  async score(): Promise<PipelineResult> {
    const { data } = await client.post("/pipeline/score", {});
    return data;
  },

  /** 仅生成报道 */
  async report(): Promise<PipelineResult> {
    const { data } = await client.post("/pipeline/report", {});
    return data;
  },

  /** 查询流水线状态 */
  async getStatus(): Promise<PipelineStatusResponse> {
    const { data } = await client.get("/pipeline/status");
    return data;
  },

  /** 导入 WeWe RSS 全部文章 */
  async importWewe(): Promise<{ ok: boolean; saved: number; total_in_rss: number }> {
    const { data } = await client.post("/pipeline/import-wewe");
    return data;
  },

  /** API 抓取公众号文章 (Just One API) */
  async crawlApi(days: number = 1): Promise<{ ok: boolean; saved: number; total: number }> {
    const { data } = await client.post("/pipeline/crawl-api", { days });
    return data;
  },

  /** V2 双维度打分（批量） */
  async scoreV2(): Promise<{ ok: boolean; total: number; scored: number; candidates: number }> {
    const { data } = await client.post("/pipeline/score-v2");
    return data;
  },

  /** V2 双维度打分（单篇） */
  async scoreV2Single(urlHash: string): Promise<{
    ok: boolean; product_relevance: number; event_impact: number;
    pr_total_score: number; is_pr_candidate: boolean;
  }> {
    const { data } = await client.post(`/pipeline/score-v2/${urlHash}`);
    return data;
  },

  /** V2 智能 PR 流水线（单文章） */
  async runV2Single(urlHash: string): Promise<{
    ok: boolean;
    url_hash: string;
    title: string;
    steps: Array<{
      phase: string;
      category?: string;
      product_relevance?: number;
      event_impact?: number;
      pr_total_score?: number;
      draft_count?: number;
    }>;
  }> {
    const { data } = await client.post(`/pipeline/run-v2/${urlHash}`);
    return data;
  },

  /** V2 智能 PR 流水线（全量） */
  async runV2(crawlDays: number = 1): Promise<PipelineResult> {
    const { data } = await client.post("/pipeline/run-v2", { crawl_days: crawlDays });
    return data;
  },

  /** V2 流水线状态 */
  async getStatusV2(): Promise<PipelineStatusResponse> {
    const { data } = await client.get("/pipeline/status-v2");
    return data;
  },

  /** V2 6分类 */
  async classifyV2(
    urlHashes?: string[],
    force: boolean = false,
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
      is_pr_eligible: boolean;
    }>;
  }> {
    const { data } = await client.post("/pipeline/classify-v2", {
      url_hashes: urlHashes || null,
      force,
    });
    return data;
  },
};

// ═══════════════════════════════════════════════════════════
// Reports API
// ═══════════════════════════════════════════════════════════

export const reportsApi = {
  /** 报道列表 */
  async getReports(
    page: number = 1,
    pageSize: number = 10,
  ): Promise<PaginatedResponse<Report>> {
    const { data } = await client.get("/reports", {
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
    const { data } = await client.get("/knowledge");
    return data;
  },
};

// ═══════════════════════════════════════════════════════════
// Accounts API（公众号账号管理）
// ═══════════════════════════════════════════════════════════

export const accountsApi = {
  /** 查询所有账号状态 */
  async getAccountStatus(): Promise<AccountStatusResult> {
    const { data } = await client.get("/accounts/status");
    return data;
  },

  /** 创建登录二维码 */
  async createQRCode(): Promise<QRCodeResult> {
    const { data } = await client.post("/accounts/qrcode");
    return data;
  },

  /** 轮询扫码结果 */
  async pollLogin(uuid: string, timeout: number = 120): Promise<PollLoginResult> {
    const { data } = await client.post("/accounts/poll-login", null, {
      params: { uuid, timeout_seconds: timeout },
    });
    return data;
  },

  /** 保存账号到 WeWe RSS */
  async saveAccount(vid: string, token: string, name: string): Promise<{ ok: boolean }> {
    const { data } = await client.post("/accounts/save", null, {
      params: { vid, token, name },
    });
    return data;
  },

  /** 启用/停用账号 */
  async toggleAccount(accountId: string, status: number): Promise<{ ok: boolean }> {
    const { data } = await client.post("/accounts/toggle", null, {
      params: { account_id: accountId, status },
    });
    return data;
  },

  /** 更新全部最新文章 */
  async refreshArticles(): Promise<{ ok: boolean; saved: number; total: number }> {
    const { data } = await client.post("/accounts/refresh");
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
    const { data } = await client.post("/chat/ask", request);
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
    const url = `${BASE_URL}/chat/ask_stream`;

    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request),
      });

      if (!response.ok) {
        const errorText = await response.text();
        onError?.(errorText || `HTTP ${response.status}`);
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) {
        onError?.("无法读取响应流");
        return;
      }

      const decoder = new TextDecoder();
      let buffer = "";

      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // 按 SSE 事件分割（双换行）
        const lines = buffer.split("\n\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
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
      onError?.(err instanceof Error ? err.message : "网络错误");
    }
  },

  /** 生成修订稿 */
  async reviseDraft(
    urlHash: string,
    draftIndex: number,
    request: DraftReviseRequest,
  ): Promise<DraftReviseResponse> {
    const { data } = await client.post(
      `/articles/${urlHash}/drafts/${draftIndex}/revise`,
      request,
    );
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
    const url = `${BASE_URL}/articles/${urlHash}/drafts/${draftIndex}/revise_stream`;

    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request),
      });

      if (!response.ok) {
        const errorText = await response.text();
        onError?.(errorText || `HTTP ${response.status}`);
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) {
        onError?.("无法读取响应流");
        return;
      }

      const decoder = new TextDecoder();
      let buffer = "";

      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split("\n\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
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
      onError?.(err instanceof Error ? err.message : "网络错误");
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

  /** 获取对话历史 */
  async getChatHistory(
    urlHash: string,
    draftIndex: number,
  ): Promise<ChatMessage[]> {
    const { data } = await client.get(
      `/articles/${urlHash}/drafts/${draftIndex}/chat-history`,
    );
    return data.data.messages;
  },

  /** 清空对话历史 */
  async clearChatHistory(
    urlHash: string,
    draftIndex: number,
  ): Promise<{ cleared: boolean }> {
    const { data } = await client.delete(
      `/articles/${urlHash}/drafts/${draftIndex}/chat-history`,
    );
    return data.data;
  },
};

// ═══════════════════════════════════════════════════════════
// 统一导出
// ═══════════════════════════════════════════════════════════

const api = {
  ...dashboardApi,
  ...pipelineApi,
  ...reportsApi,
  ...accountsApi,
  ...chatApi,
};

export default api;
