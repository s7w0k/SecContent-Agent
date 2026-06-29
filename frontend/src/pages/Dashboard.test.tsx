import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import Dashboard from "./Dashboard";

// Mock all child components and API
vi.mock("../api/client", () => ({
  default: {
    getStats: vi.fn().mockResolvedValue({
      total_articles: 100,
      ai_security_count: 30,
      high_value_count: 10,
      source_distribution: {},
      category_distribution: { "MCP协议漏洞": 5 },
    }),
    getArticles: vi.fn().mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      page_size: 20,
      pages: 0,
    }),
    getReports: vi.fn().mockResolvedValue({
      items: [],
      total: 5,
      page: 1,
      page_size: 1,
      pages: 1,
    }),
    getArticle: vi.fn().mockResolvedValue({}),
    getStatus: vi.fn().mockResolvedValue({ status: "idle", state: {}, errors: [] }),
    run: vi.fn(),
    crawl: vi.fn(),
    score: vi.fn(),
    report: vi.fn(),
  },
}));

vi.mock("../components/StatsCards", () => ({
  default: () => <div>StatsCards Mock</div>,
}));
vi.mock("../components/FilterBar", () => ({
  default: () => <div>FilterBar Mock</div>,
}));
vi.mock("../components/ArticleTable", () => ({
  default: () => <div>ArticleTable Mock</div>,
}));
vi.mock("../components/PipelineControl", () => ({
  default: ({ onComplete }: { onComplete: () => void }) => (
    <div>PipelineControl Mock</div>
  ),
}));
vi.mock("../components/ReportViewer", () => ({
  default: () => <div>ReportViewer Mock</div>,
}));

describe("Dashboard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders page title", () => {
    render(<Dashboard />);
    expect(screen.getByText("🚀 PR Agent Dashboard")).toBeDefined();
  });

  it("renders all child components", () => {
    render(<Dashboard />);
    expect(screen.getByText("StatsCards Mock")).toBeDefined();
    expect(screen.getByText("FilterBar Mock")).toBeDefined();
    expect(screen.getByText("ArticleTable Mock")).toBeDefined();
    expect(screen.getByText("PipelineControl Mock")).toBeDefined();
  });
});
