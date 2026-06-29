import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import App from "./App";

// Mock Dashboard page to avoid heavy sub-component loading
vi.mock("./pages/Dashboard", () => ({
  default: () => <div>Dashboard Page</div>,
}));

describe("App", () => {
  it("renders app header", () => {
    render(<App />);
    expect(screen.getByText("🛡 PR Agent")).toBeDefined();
  });

  it("shows dashboard tab by default", () => {
    render(<App />);
    expect(screen.getByText("仪表盘")).toBeDefined();
    expect(screen.getByText("关于")).toBeDefined();
  });

  it("shows dashboard content by default", () => {
    render(<App />);
    expect(screen.getByText("Dashboard Page")).toBeDefined();
  });
});
