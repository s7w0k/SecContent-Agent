/**
 * API Client — 单元测试
 *
 * 运行:
 *   cd frontend && npx vitest run src/api/client.test.ts
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import axios from "axios";
import type { AxiosInstance } from "axios";

// Mock axios
vi.mock("axios", () => {
  const mockGet = vi.fn();
  const mockPost = vi.fn();
  const mockDelete = vi.fn();
  const mockAxios = {
    create: vi.fn(() => ({
      get: mockGet,
      post: mockPost,
      delete: mockDelete,
      interceptors: {
        response: { use: vi.fn() },
        request: { use: vi.fn() },
      },
    })),
  };
  return { default: mockAxios };
});

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockDelete = vi.fn();

// Re-create client with controlled mocks
let api: ReturnType<typeof import("./client").default>;

async function setupApi() {
  // Clear the module cache to re-import with fresh mocks
  vi.resetModules();

  // Override the create mock
  (axios.create as ReturnType<typeof vi.fn>).mockReturnValue({
    get: mockGet,
    post: mockPost,
    delete: mockDelete,
    interceptors: {
      response: { use: vi.fn() },
      request: { use: vi.fn() },
    },
  } as unknown as AxiosInstance);

  const mod = await import("./client");
  api = mod.default;
}

// ═══════════════════════════════════════════════════════════
// Dashboard API
// ═══════════════════════════════════════════════════════════

describe("Dashboard API", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await setupApi();
  });

  it("getArticles calls GET /articles with query params", async () => {
    const mockData = { items: [], total: 0, page: 1, page_size: 20, pages: 0 };
    mockGet.mockResolvedValueOnce({ data: mockData });

    const result = await api.getArticles({ page: 1, source_type: "overseas_news" });
    expect(mockGet).toHaveBeenCalledWith("/articles", {
      params: { page: 1, source_type: "overseas_news" },
    });
    expect(result).toEqual(mockData);
  });

  it("getArticles handles empty query", async () => {
    mockGet.mockResolvedValueOnce({
      data: { items: [], total: 0, page: 1, page_size: 20, pages: 0 },
    });
    await api.getArticles();
    expect(mockGet).toHaveBeenCalledWith("/articles", { params: {} });
  });

  it("getArticle calls GET /articles/:hash", async () => {
    const article = { _id: "1", title: "Test", url_hash: "abc" } as any;
    mockGet.mockResolvedValueOnce({ data: article });

    const result = await api.getArticle("abc");
    expect(mockGet).toHaveBeenCalledWith("/articles/abc");
    expect(result).toEqual(article);
  });

  it("getStats returns stats data", async () => {
    const stats = { total_articles: 42, ai_security_count: 10, high_value_count: 5 };
    mockGet.mockResolvedValueOnce({ data: stats });

    const result = await api.getStats();
    expect(mockGet).toHaveBeenCalledWith("/stats");
    expect(result.total_articles).toBe(42);
  });
});

// ═══════════════════════════════════════════════════════════
// Pipeline API
// ═══════════════════════════════════════════════════════════

describe("Pipeline API", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await setupApi();
  });

  it("run calls POST /pipeline/run", async () => {
    const mockResult = { pipeline_id: "p1", status: "completed", state: {} };
    mockPost.mockResolvedValueOnce({ data: mockResult });

    const result = await api.run(3);
    expect(mockPost).toHaveBeenCalledWith("/pipeline/run", { crawl_days: 3 });
    expect(result.status).toBe("completed");
  });

  it("crawl calls POST /pipeline/crawl", async () => {
    mockPost.mockResolvedValueOnce({ data: { pipeline_id: "p2", status: "completed" } });
    await api.crawl(2);
    expect(mockPost).toHaveBeenCalledWith("/pipeline/crawl", { crawl_days: 2 });
  });

  it("score calls POST /pipeline/score", async () => {
    mockPost.mockResolvedValueOnce({ data: { pipeline_id: "p3", status: "completed" } });
    await api.score();
    expect(mockPost).toHaveBeenCalledWith("/pipeline/score", {});
  });

  it("report calls POST /pipeline/report", async () => {
    mockPost.mockResolvedValueOnce({ data: { pipeline_id: "p4", status: "completed" } });
    await api.report();
    expect(mockPost).toHaveBeenCalledWith("/pipeline/report", {});
  });

  it("getStatus calls GET /pipeline/status", async () => {
    const status = { status: "idle", current_phase: "", errors: [] };
    mockGet.mockResolvedValueOnce({ data: status });

    const result = await api.getStatus();
    expect(mockGet).toHaveBeenCalledWith("/pipeline/status");
    expect(result.status).toBe("idle");
  });
});

// ═══════════════════════════════════════════════════════════
// Reports API
// ═══════════════════════════════════════════════════════════

describe("Reports API", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await setupApi();
  });

  it("getReports calls GET /reports with pagination", async () => {
    const mockData = { items: [], total: 0, page: 1, page_size: 10, pages: 0 };
    mockGet.mockResolvedValueOnce({ data: mockData });

    await api.getReports(2, 10);
    expect(mockGet).toHaveBeenCalledWith("/reports", {
      params: { page: 2, page_size: 10 },
    });
  });

  it("getReport calls GET /reports/:id", async () => {
    const report = { _id: "r1", title: "PR Report", content_md: "# Report" } as any;
    mockGet.mockResolvedValueOnce({ data: report });

    const result = await api.getReport("r1");
    expect(mockGet).toHaveBeenCalledWith("/reports/r1");
    expect(result.title).toBe("PR Report");
  });

  it("getKnowledge calls GET /knowledge", async () => {
    mockGet.mockResolvedValueOnce({
      data: { loaded: true, product_name: "测试", features_count: 3 },
    });

    const result = await api.getKnowledge();
    expect(mockGet).toHaveBeenCalledWith("/knowledge");
    expect(result.loaded).toBe(true);
  });
});

// ═══════════════════════════════════════════════════════════
// Chat API
// ═══════════════════════════════════════════════════════════

describe("Chat API", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await setupApi();
  });

  it("ask calls POST /chat/ask", async () => {
    const mockResponse = {
      ok: true,
      data: { answer: "测试回答", references: ["knowledge"] },
    };
    mockPost.mockResolvedValueOnce({ data: mockResponse });

    const { chatApi } = await import("./client");
    const result = await chatApi.ask({ message: "问题" });

    expect(mockPost).toHaveBeenCalledWith("/chat/ask", { message: "问题" });
    expect(result.answer).toBe("测试回答");
    expect(result.references).toEqual(["knowledge"]);
  });

  it("reviseDraft calls POST /articles/:hash/drafts/:index/revise", async () => {
    const mockResponse = {
      ok: true,
      data: {
        revision_id: "rev-001",
        revised_content_md: "# 新稿",
        change_summary: ["修改1"],
        saved: true,
      },
    };
    mockPost.mockResolvedValueOnce({ data: mockResponse });

    const { chatApi } = await import("./client");
    const result = await chatApi.reviseDraft("abc123", 0, {
      instruction: "改意见",
      save: true,
    });

    expect(mockPost).toHaveBeenCalledWith(
      "/articles/abc123/drafts/0/revise",
      { instruction: "改意见", save: true },
    );
    expect(result.revision_id).toBe("rev-001");
    expect(result.saved).toBe(true);
  });

  it("applyRevision calls POST /articles/:hash/drafts/:index/revisions/:id/apply", async () => {
    const mockResponse = {
      ok: true,
      data: {
        article_url_hash: "abc123",
        draft_index: 0,
        revision_id: "rev-001",
        applied: true,
      },
    };
    mockPost.mockResolvedValueOnce({ data: mockResponse });

    const { chatApi } = await import("./client");
    const result = await chatApi.applyRevision("abc123", 0, "rev-001");

    expect(mockPost).toHaveBeenCalledWith(
      "/articles/abc123/drafts/0/revisions/rev-001/apply",
    );
    expect(result.applied).toBe(true);
  });

  it("getChatHistory calls GET /articles/:hash/drafts/:index/chat-history", async () => {
    const mockResponse = {
      ok: true,
      data: {
        messages: [
          { role: "user", content: "问题", created_at: "2026-07-07T10:00:00" },
          { role: "assistant", content: "回答", created_at: "2026-07-07T10:00:01" },
        ],
      },
    };
    mockGet.mockResolvedValueOnce({ data: mockResponse });

    const { chatApi } = await import("./client");
    const result = await chatApi.getChatHistory("abc123", 0);

    expect(mockGet).toHaveBeenCalledWith(
      "/articles/abc123/drafts/0/chat-history",
    );
    expect(result).toHaveLength(2);
    expect(result[0].role).toBe("user");
    expect(result[1].role).toBe("assistant");
  });

  it("clearChatHistory calls DELETE /articles/:hash/drafts/:index/chat-history", async () => {
    const mockResponse = {
      ok: true,
      data: { cleared: true },
    };
    mockDelete.mockResolvedValueOnce({ data: mockResponse });

    const { chatApi } = await import("./client");
    const result = await chatApi.clearChatHistory("abc123", 0);

    expect(mockDelete).toHaveBeenCalledWith(
      "/articles/abc123/drafts/0/chat-history",
    );
    expect(result.cleared).toBe(true);
  });
});
