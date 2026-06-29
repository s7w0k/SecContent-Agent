import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import StatsCards from "./StatsCards";

describe("StatsCards", () => {
  const mockStats = {
    total_articles: 150,
    ai_security_count: 45,
    high_value_count: 12,
    source_distribution: {},
    category_distribution: {},
  };

  it("renders 4 stat cards", () => {
    render(<StatsCards stats={mockStats} loading={false} reportCount={8} />);
    expect(screen.getByText("总文章数")).toBeDefined();
    expect(screen.getByText("AI 安全相关")).toBeDefined();
    expect(screen.getByText(/高价值文章/)).toBeDefined();
    expect(screen.getByText("已生成报道")).toBeDefined();
  });

  it("displays correct stat values", () => {
    render(<StatsCards stats={mockStats} loading={false} reportCount={8} />);
    expect(screen.getByText("150")).toBeDefined();
    expect(screen.getByText("45")).toBeDefined();
    expect(screen.getByText("12")).toBeDefined();
    expect(screen.getByText("8")).toBeDefined();
  });

  it("renders skeleton when loading", () => {
    const { container } = render(
      <StatsCards stats={null} loading={true} reportCount={0} />,
    );
    // Skeleton renders ant-skeleton elements
    const skeletons = container.querySelectorAll(".ant-skeleton");
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it("handles null stats gracefully", () => {
    render(<StatsCards stats={null} loading={false} reportCount={0} />);
    // Should show 0 for all values
    const zeros = screen.getAllByText("0");
    expect(zeros.length).toBeGreaterThanOrEqual(3);
  });
});
