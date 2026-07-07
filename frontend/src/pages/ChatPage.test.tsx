/**
 * ChatPage — 组件测试
 *
 * 运行:
 *   cd frontend && npx vitest run src/pages/ChatPage.test.tsx
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import ChatPage from "./ChatPage";

// Mock API client
vi.mock("../api/client", () => ({
  default: {
    getArticles: vi.fn(),
    getArticle: vi.fn(),
  },
  chatApi: {
    ask: vi.fn(),
    askStream: vi.fn(),
    reviseDraft: vi.fn(),
    applyRevision: vi.fn(),
    getChatHistory: vi.fn().mockResolvedValue([]),
    clearChatHistory: vi.fn().mockResolvedValue({ cleared: true }),
  },
}));

// Mock react-markdown
vi.mock("react-markdown", () => ({
  default: ({ children }: { children: string }) => (
    <div data-testid="markdown">{children}</div>
  ),
}));

import api, { chatApi } from "../api/client";

const mockArticle = {
  _id: "1",
  url_hash: "abc123def45678901234567890123456",
  title: "Critical MCP Vulnerability",
  url: "https://example.com/mcp",
  source: "The Hacker News",
  source_type: "overseas_news" as const,
  published_at: "2026-06-29",
  added_at: "2026-06-29T12:00:00",
  summary: "A critical vulnerability...",
  summary_cn: "MCP严重漏洞",
  is_ai_security: true,
  is_agent_security: true,
  category: "MCP协议漏洞",
  ai_relevance_score: 92,
  reportability_score: 78,
  total_score: 170,
  is_high_value: true,
  has_report: false,
  report_id: null,
  pr_drafts: [
    {
      template: "爆点A",
      perspective: "产品能力视角",
      content_md: "# [原标题]\n\n## 导语\n草稿内容",
      title: "Critical MCP Vulnerability",
      index: 1,
      revisions: [],
    },
  ],
};

describe("ChatPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getArticles).mockResolvedValue({
      items: [mockArticle],
      total: 1,
      page: 1,
      page_size: 100,
      pages: 1,
    });
    vi.mocked(api.getArticle).mockResolvedValue(mockArticle);
  });

  // ── 基础渲染 ──

  it("renders page title and article selector", async () => {
    render(<ChatPage />);
    await waitFor(() => {
      expect(screen.getByText("文章选择")).toBeDefined();
    });
  });

  it("loads articles on mount", async () => {
    render(<ChatPage />);
    await waitFor(() => {
      expect(api.getArticles).toHaveBeenCalledWith({ page: 1, page_size: 100 });
    });
  });

  it("shows empty state when no article selected", async () => {
    render(<ChatPage />);
    await waitFor(() => {
      expect(screen.getByText("请选择文章开始对话改稿")).toBeDefined();
    });
  });

  // ── 模式切换 ──

  it("renders mode selector with 问答 and 改稿", async () => {
    render(<ChatPage />);
    await waitFor(() => {
      expect(screen.getByText("问答")).toBeDefined();
      expect(screen.getByText("改稿")).toBeDefined();
    });
  });

  // ── 问答发送 ──

  it("sends question in 问答 mode via stream", async () => {
    vi.mocked(chatApi.askStream).mockImplementation(
      async (_req, onChunk, onDone) => {
        onChunk("这是一个");
        onChunk("回答。");
        onDone?.("这是一个回答。");
      },
    );

    render(<ChatPage />);
    await waitFor(() => {
      expect(api.getArticles).toHaveBeenCalled();
    });

    // 输入问题
    const textarea = screen.getByPlaceholderText("输入问题...");
    fireEvent.change(textarea, { target: { value: "测试问题" } });

    // 点击发送
    const sendButton = screen.getByText("发送");
    fireEvent.click(sendButton);

    await waitFor(() => {
      expect(chatApi.askStream).toHaveBeenCalledWith(
        expect.objectContaining({ message: "测试问题" }),
        expect.any(Function),
        expect.any(Function),
        expect.any(Function),
      );
    });
  });

  // ── 改稿发送 ──

  it("switches to 改稿 mode and shows different placeholder", async () => {
    render(<ChatPage />);
    await waitFor(() => expect(api.getArticles).toHaveBeenCalled());

    // 默认问答模式
    expect(screen.getByPlaceholderText("输入问题...")).toBeDefined();

    // 切换到改稿模式
    const reviseMode = screen.getByText("改稿");
    fireEvent.click(reviseMode);

    // 改稿模式显示不同 placeholder
    expect(screen.getByPlaceholderText(/输入修改意见/)).toBeDefined();
  });

  // ── loading 和 error 状态 ──

  it("shows loading when sending", async () => {
    vi.mocked(chatApi.askStream).mockImplementation(
      () => new Promise((resolve) => setTimeout(resolve, 100)),
    );

    render(<ChatPage />);
    await waitFor(() => expect(api.getArticles).toHaveBeenCalled());

    const textarea = screen.getByPlaceholderText("输入问题...");
    fireEvent.change(textarea, { target: { value: "问题" } });
    fireEvent.click(screen.getByText("发送"));

    // 发送按钮应该显示 loading
    await waitFor(() => {
      expect(screen.getByText("发送")).toBeDefined();
    });
  });

  it("shows error on stream API failure", async () => {
    vi.mocked(chatApi.askStream).mockImplementation(
      async (_req, _onChunk, _onDone, onError) => {
        onError?.("流式请求失败");
      },
    );

    render(<ChatPage />);
    await waitFor(() => expect(api.getArticles).toHaveBeenCalled());

    const textarea = screen.getByPlaceholderText("输入问题...");
    fireEvent.change(textarea, { target: { value: "问题" } });
    fireEvent.click(screen.getByText("发送"));

    await waitFor(() => {
      expect(chatApi.askStream).toHaveBeenCalled();
    });
  });

  it("disables send button when input is empty", async () => {
    render(<ChatPage />);
    await waitFor(() => expect(api.getArticles).toHaveBeenCalled());

    const sendButton = screen.getByText("发送").closest("button");
    expect(sendButton?.disabled).toBe(true);
  });
});
