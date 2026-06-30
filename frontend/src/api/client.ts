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
  Article,
  ArticleQuery,
  KnowledgeSummary,
  PaginatedResponse,
  PipelineResult,
  PipelineStatusResponse,
  Report,
  StatsData,
} from "../types";

// ═══════════════════════════════════════════════════════════
// Axios 实例
// ═══════════════════════════════════════════════════════════

const BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api";

const client: AxiosInstance = axios.create({
  baseURL: BASE_URL,
  timeout: 30000,
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

  /** 仅爬取 */
  async crawl(crawlDays: number = 1): Promise<PipelineResult> {
    const { data } = await client.post("/pipeline/crawl", {
      crawl_days: crawlDays,
    });
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
// 统一导出
// ═══════════════════════════════════════════════════════════

const api = {
  ...dashboardApi,
  ...pipelineApi,
  ...reportsApi,
};

export default api;
