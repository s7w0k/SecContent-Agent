/**
 * Vitest 全局测试配置
 *
 * 在每个测试文件之前自动加载，提供：
 *   - jest-dom 扩展匹配器 (toBeInTheDocument 等)
 *   - 全局 Mock 清理
 */

import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// 每个测试后自动清理 DOM
afterEach(() => {
  cleanup();
});

// Mock window.matchMedia（Ant Design 需要）
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

// Mock navigator.clipboard（ReportViewer 复制功能）
Object.defineProperty(navigator, "clipboard", {
  writable: true,
  value: {
    writeText: vi.fn().mockResolvedValue(undefined),
    readText: vi.fn().mockResolvedValue(""),
  },
});

// Mock URL.createObjectURL / revokeObjectURL（ReportViewer 下载功能）
global.URL.createObjectURL = vi.fn(() => "blob:test");
global.URL.revokeObjectURL = vi.fn();

// 抑制 Ant Design 的 React 18 严格模式警告
const originalError = console.error;
console.error = (...args: unknown[]) => {
  const msg = String(args[0]);
  if (
    msg.includes("ReactDOM.render") ||
    msg.includes("findDOMNode")
  ) {
    return;
  }
  originalError.call(console, ...args);
};
