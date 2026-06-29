import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import ReportViewer from "./ReportViewer";
import type { Article } from "../types";

// Mock the API client
vi.mock("../api/client", () => ({
  default: {
    getReport: vi.fn().mockResolvedValue({
      _id: "rpt-1",
      article_url_hash: "abc123",
      title: "Critical MCP Vulnerability Found",
      content_md: "# Test Report\n\n## 导语\nTest content.",
      template: "standard_pr",
      scores: { relevance: 92, reportability: 78 },
      generated_by: "pr-agent-pipeline",
      created_at: "2026-06-29 12:00:00",
    }),
  },
}));

// Mock react-markdown
vi.mock("react-markdown", () => ({
  default: ({ children }: { children: string }) => <div>{children}</div>,
}));

const mockArticle: Article = {
  _id: "1",
  url_hash: "abc123",
  title: "Test Article",
  url: "https://example.com",
  source: "The Hacker News",
  source_type: "overseas_news",
  published_at: "2026-06-29",
  added_at: "2026-06-29",
  summary: "Test summary",
  summary_cn: "测试",
  is_ai_security: true,
  is_agent_security: true,
  category: "MCP协议漏洞",
  ai_relevance_score: 92,
  reportability_score: 78,
  total_score: 170,
  is_high_value: true,
  has_report: true,
  report_id: "rpt-1",
};

describe("ReportViewer", () => {
  it("renders nothing when reportId is null", () => {
    const { container } = render(
      <ReportViewer reportId={null} article={null} onClose={vi.fn()} />,
    );
    expect(container.innerHTML).toBe("");
  });

  it("opens modal when reportId is set", () => {
    render(
      <ReportViewer reportId="rpt-1" article={mockArticle} onClose={vi.fn()} />,
    );
    expect(screen.getByText("PR 报道")).toBeDefined();
  });

  it("shows copy and download buttons in footer", () => {
    render(
      <ReportViewer reportId="rpt-1" article={mockArticle} onClose={vi.fn()} />,
    );
    expect(screen.getByText("复制全文")).toBeDefined();
    expect(screen.getByText("下载 .md")).toBeDefined();
  });
});
