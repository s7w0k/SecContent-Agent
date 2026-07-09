import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import PipelineControl from "./PipelineControl";

// Mock the API client
vi.mock("../api/client", () => ({
  default: {
    run: vi.fn(),
    crawl: vi.fn(),
    score: vi.fn(),
    report: vi.fn(),
    getStatus: vi.fn().mockResolvedValue({
      status: "idle",
      current_phase: "",
      state: {},
      errors: [],
    }),
  },
}));

describe("PipelineControl", () => {
  const defaultProps = {
    onComplete: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders trigger buttons", () => {
    render(<PipelineControl {...defaultProps} />);
    expect(screen.getByText("全流程")).toBeDefined();
    expect(screen.getByText("爬取+分类")).toBeDefined();
    expect(screen.getByText("V2打分")).toBeDefined();
    expect(screen.getByText("仅报道")).toBeDefined();
  });

  it("shows idle status initially", () => {
    render(<PipelineControl {...defaultProps} />);
    expect(screen.getByText(/空闲/)).toBeDefined();
  });

  it("shows hint when no state", () => {
    render(<PipelineControl {...defaultProps} />);
    expect(screen.getByText(/点击.*全流程.*开始/)).toBeDefined();
  });

  it("shows pipeline control title", () => {
    render(<PipelineControl {...defaultProps} />);
    expect(screen.getByText("流水线控制")).toBeDefined();
  });
});
