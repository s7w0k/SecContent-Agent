import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import App from "./App";

describe("App", () => {
  it("renders dashboard title", () => {
    render(<App />);
    expect(screen.getByText("🚀 PR Agent Dashboard")).toBeDefined();
  });

  it("renders service list", () => {
    render(<App />);
    expect(screen.getByText(/MongoDB/)).toBeDefined();
    expect(screen.getByText(/mcp-wewe/)).toBeDefined();
    expect(screen.getByText(/mcp-crawl/)).toBeDefined();
    expect(screen.getByText(/Backend/)).toBeDefined();
  });
});
