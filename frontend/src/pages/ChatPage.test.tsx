/**
 * ChatPage — 组件测试
 *
 * 运行:
 *   cd frontend && npx vitest run src/pages/ChatPage.test.tsx
 */

import { readFileSync } from 'node:fs';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ChatPage from './ChatPage';

const chatPageCss = readFileSync('src/pages/ChatPage.module.css', 'utf8');

// Mock API client
vi.mock('../api/client', () => ({
  default: {
    getArticles: vi.fn(),
    getArticle: vi.fn(),
    log: vi.fn().mockResolvedValue({ activity_id: 'activity-1', created_at: 'now' }),
  },
  chatApi: {
    ask: vi.fn(),
    askStream: vi.fn(),
    reviseDraft: vi.fn(),
    reviseDraftStream: vi.fn(),
    applyRevision: vi.fn(),
    reviewDraft: vi.fn(),
    getChatHistory: vi.fn().mockResolvedValue([]),
    clearChatHistory: vi.fn().mockResolvedValue({ cleared: true }),
  },
}));

// Mock react-markdown
vi.mock('react-markdown', () => ({
  default: ({ children }: { children: string }) => <div data-testid="markdown">{children}</div>,
}));

import api, { chatApi } from '../api/client';

const mockArticle = {
  _id: '1',
  url_hash: 'abc123def45678901234567890123456',
  title: 'Critical MCP Vulnerability',
  url: 'https://example.com/mcp',
  source: 'The Hacker News',
  source_type: 'overseas_news' as const,
  published_at: '2026-06-29',
  added_at: '2026-06-29T12:00:00',
  summary: 'A critical vulnerability...',
  summary_cn: 'MCP严重漏洞',
  is_ai_security: true,
  is_agent_security: true,
  category: 'MCP协议漏洞',
  ai_relevance_score: 92,
  reportability_score: 78,
  total_score: 170,
  is_high_value: true,
  has_report: false,
  report_id: null,
  pr_drafts: [
    {
      template: '爆点A',
      perspective: '产品能力视角',
      content_md: '# [原标题]\n\n## 导语\n草稿内容',
      title: 'Critical MCP Vulnerability',
      index: 1,
      review: {
        status: 'completed' as const,
        content_hash: '0'.repeat(64),
        summary: '发现 1 个建议修改问题',
        issues: [
          {
            issue_id: 'issue-001',
            category: 'absolute_claim' as const,
            severity: 'medium' as const,
            quote: '业内第一',
            reason: '缺少排名依据',
            suggestion: '删除绝对化表达',
          },
        ],
        counts: { high: 0, medium: 1, low: 0 },
        fact_check_available: true,
        reviewed_at: '2026-07-22T08:00:00Z',
      },
      revisions: [
        {
          revision_id: 'rev-001',
          instruction: '强化标题',
          content_md: '# 修订标题\n\n修订稿内容',
          change_summary: ['标题更有冲击力'],
          created_at: '2026-07-21T10:00:00Z',
          created_by: 'local-user',
          applied: false,
        },
      ],
    },
  ],
};

describe('ChatPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 });
    Object.defineProperty(window, 'PointerEvent', {
      configurable: true,
      value: MouseEvent,
    });
    vi.mocked(api.getArticles).mockResolvedValue({
      items: [mockArticle],
      total: 1,
      page: 1,
      page_size: 100,
      pages: 1,
    });
    vi.mocked(api.getArticle).mockResolvedValue(mockArticle);
  });

  async function selectArticle() {
    // 文章选择区域是 Collapse 内的 List，点击文章标题行即可选中
    const articleTitle = await screen.findByText(mockArticle.title);
    fireEvent.click(articleTitle);
    await waitFor(() =>
      expect(chatApi.getChatHistory).toHaveBeenCalledWith(mockArticle.url_hash, 0),
    );
  }

  // ── 基础渲染 ──

  it('renders page title and article selector', async () => {
    render(<ChatPage />);
    await waitFor(() => {
      expect(screen.getByText('文章选择')).toBeDefined();
    });
  });

  it('loads articles on mount', async () => {
    render(<ChatPage />);
    await waitFor(() => {
      expect(api.getArticles).toHaveBeenCalledWith(
        expect.objectContaining({
          page: 1,
          page_size: 100,
          has_drafts: true,
          sort_by: 'added_at',
          order: 'desc',
        }),
      );
    });
  });

  it('shows empty state when no article selected', async () => {
    render(<ChatPage />);
    await waitFor(() => {
      expect(screen.getByText('请选择文章开始对话改稿')).toBeDefined();
    });
  });

  // ── 模式切换 ──

  it('renders mode selector with 问答 and 改稿', async () => {
    render(<ChatPage />);
    await waitFor(() => {
      expect(screen.getByText('问答')).toBeDefined();
      expect(screen.getByText('改稿')).toBeDefined();
    });
  });

  // ── 问答发送 ──

  it('sends question in 问答 mode via stream', async () => {
    vi.mocked(chatApi.askStream).mockImplementation(async (_req, onChunk, onDone) => {
      onChunk('这是一个');
      onChunk('回答。');
      onDone?.('这是一个回答。');
    });

    render(<ChatPage />);
    await waitFor(() => {
      expect(api.getArticles).toHaveBeenCalled();
    });

    // 输入问题
    const textarea = screen.getByPlaceholderText('输入问题...');
    fireEvent.change(textarea, { target: { value: '测试问题' } });

    // 点击发送
    const sendButton = screen.getByText('发送');
    fireEvent.click(sendButton);

    await waitFor(() => {
      expect(chatApi.askStream).toHaveBeenCalledWith(
        expect.objectContaining({ message: '测试问题' }),
        expect.any(Function),
        expect.any(Function),
        expect.any(Function),
        expect.any(Function),
        expect.any(AbortSignal),
      );
    });
  });

  // ── 改稿发送 ──

  it('switches to 改稿 mode and shows different placeholder', async () => {
    render(<ChatPage />);
    await waitFor(() => expect(api.getArticles).toHaveBeenCalled());

    // 默认问答模式
    expect(screen.getByPlaceholderText('输入问题...')).toBeDefined();

    // 切换到改稿模式
    const reviseMode = screen.getByText('改稿');
    fireEvent.click(reviseMode);

    // 改稿模式显示不同 placeholder
    expect(screen.getByPlaceholderText(/输入修改意见/)).toBeDefined();
  });

  // ── loading 和 error 状态 ──

  it('shows loading when sending', async () => {
    vi.mocked(chatApi.askStream).mockImplementation(
      () => new Promise((resolve) => setTimeout(resolve, 100)),
    );

    render(<ChatPage />);
    await waitFor(() => expect(api.getArticles).toHaveBeenCalled());

    const textarea = screen.getByPlaceholderText('输入问题...');
    fireEvent.change(textarea, { target: { value: '问题' } });
    fireEvent.click(screen.getByText('发送'));

    // 发送按钮应该显示 loading
    await waitFor(() => {
      expect(screen.getByText('发送')).toBeDefined();
    });
  });

  it('shows error on stream API failure', async () => {
    vi.mocked(chatApi.askStream).mockImplementation(async (_req, _onChunk, _onDone, onError) => {
      onError?.('流式请求失败');
    });

    render(<ChatPage />);
    await waitFor(() => expect(api.getArticles).toHaveBeenCalled());

    const textarea = screen.getByPlaceholderText('输入问题...');
    fireEvent.change(textarea, { target: { value: '问题' } });
    fireEvent.click(screen.getByText('发送'));

    await waitFor(() => {
      expect(chatApi.askStream).toHaveBeenCalled();
    });
  });

  it('disables send button when input is empty', async () => {
    render(<ChatPage />);
    await waitFor(() => expect(api.getArticles).toHaveBeenCalled());

    const sendButton = screen.getByText('发送').closest('button');
    expect(sendButton?.disabled).toBe(true);
  });

  // ── 阶段八稿件区域扩展 ──

  it('uses a resizable default sider and provides collapsible article and draft selectors', async () => {
    const { container } = render(<ChatPage />);
    await selectArticle();

    const sider = container.querySelector('.ant-layout-sider');
    expect(sider).toHaveStyle({ flex: '0 0 760px', maxWidth: '760px', minWidth: '760px' });
    expect(screen.getByRole('separator', { name: '调整文章选择栏宽度' })).toHaveAttribute(
      'aria-valuenow',
      '760',
    );

    const articleHeader = screen.getByRole('button', { name: /文章选择/ });
    const draftHeader = screen.getByRole('button', { name: /草稿选择/ });
    expect(articleHeader).toHaveAttribute('aria-expanded', 'true');
    expect(draftHeader).toHaveAttribute('aria-expanded', 'true');

    fireEvent.click(articleHeader);
    fireEvent.click(draftHeader);
    expect(articleHeader).toHaveAttribute('aria-expanded', 'false');
    expect(draftHeader).toHaveAttribute('aria-expanded', 'false');
  });

  it('supports pointer and keyboard resizing and persists the selected width', async () => {
    const { container, unmount } = render(<ChatPage />);
    await waitFor(() => expect(api.getArticles).toHaveBeenCalled());
    const handle = screen.getByRole('separator', { name: '调整文章选择栏宽度' });

    fireEvent.pointerDown(handle, { button: 0, clientX: 760, pointerId: 1 });
    fireEvent.pointerMove(handle, { clientX: 900, pointerId: 1 });

    expect(container.querySelector('.ant-layout-sider')).toHaveStyle({ width: '900px' });
    expect(handle).toHaveAttribute('aria-valuenow', '900');
    expect(window.localStorage.getItem('chat-page-sider-width')).toBeNull();

    fireEvent.pointerUp(handle, { clientX: 900, pointerId: 1 });

    expect(container.querySelector('.ant-layout-sider')).toHaveStyle({ width: '900px' });
    expect(handle).toHaveAttribute('aria-valuenow', '900');
    expect(window.localStorage.getItem('chat-page-sider-width')).toBe('900');

    fireEvent.keyDown(handle, { key: 'ArrowLeft' });
    expect(handle).toHaveAttribute('aria-valuenow', '880');
    expect(window.localStorage.getItem('chat-page-sider-width')).toBe('880');

    unmount();
    const restored = render(<ChatPage />);
    await waitFor(() => expect(api.getArticles).toHaveBeenCalled());
    expect(restored.container.querySelector('.ant-layout-sider')).toHaveStyle({ width: '880px' });
  });

  it('uses the available layout width instead of a fixed 1000px sider limit', async () => {
    const { container } = render(<ChatPage />);
    await waitFor(() => expect(api.getArticles).toHaveBeenCalled());
    const handle = screen.getByRole('separator', { name: '调整文章选择栏宽度' });

    fireEvent.pointerDown(handle, { button: 0, clientX: 760, pointerId: 2 });
    fireEvent.pointerMove(handle, { clientX: 1600, pointerId: 2 });
    fireEvent.pointerUp(handle, { clientX: 1600, pointerId: 2 });

    expect(container.querySelector('.ant-layout-sider')).toHaveStyle({ width: '1150px' });
    expect(handle).toHaveAttribute('aria-valuemax', '1150');
    expect(handle).toHaveAttribute('aria-valuenow', '1150');
  });

  it('keeps the stacked responsive layout and hides the desktop resize handle', () => {
    expect(chatPageCss).toContain('@media (max-width: 991px)');
    expect(chatPageCss).toContain('flex-direction: column !important');
    expect(chatPageCss).toContain('min-width: 280px');
    expect(chatPageCss).toContain('transition: none !important');
    expect(chatPageCss).toContain('.resizeHandle');
    expect(chatPageCss).toContain('display: none');
  });

  it('opens a 60% full-preview drawer and supports copying and downloading the current draft', async () => {
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => undefined);
    render(<ChatPage />);
    await selectArticle();

    fireEvent.click(screen.getByRole('button', { name: '全屏预览' }));

    const dialog = await screen.findByRole('dialog');
    expect(screen.getByText(`${mockArticle.title} - 爆点A`)).toBeInTheDocument();
    expect(document.querySelector('.ant-drawer-content-wrapper')).toHaveStyle({ width: '60%' });
    expect(within(dialog).getByTestId('markdown')).toHaveTextContent('草稿内容');

    fireEvent.click(within(dialog).getByRole('button', { name: /复制/ }));
    await waitFor(() =>
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith('# [原标题]\n\n## 导语\n草稿内容'),
    );

    fireEvent.click(within(dialog).getByRole('button', { name: /下载/ }));
    expect(URL.createObjectURL).toHaveBeenCalled();
    expect(api.log).toHaveBeenCalledWith(expect.objectContaining({ action: 'draft_download' }));
    anchorClick.mockRestore();
  });

  it('shows the review panel and supports manually rechecking the current draft', async () => {
    vi.mocked(chatApi.reviewDraft).mockResolvedValue(mockArticle.pr_drafts[0].review);
    render(<ChatPage />);
    await selectArticle();

    expect(screen.getByText('内容与话术检查')).toBeInTheDocument();
    expect(screen.getByText('建议修改 1')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /重新检查/ }));

    await waitFor(() => expect(chatApi.reviewDraft).toHaveBeenCalledWith(mockArticle.url_hash, 0));
  });

  it('shows and executes apply revision from the full-preview drawer', async () => {
    vi.mocked(chatApi.applyRevision).mockResolvedValue({
      article_url_hash: mockArticle.url_hash,
      draft_index: 0,
      revision_id: 'rev-001',
      applied: true,
    });
    render(<ChatPage />);
    await selectArticle();

    fireEvent.click(screen.getByRole('button', { name: /查看/ }));
    fireEvent.click(screen.getByRole('button', { name: '全屏预览' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: '应用为当前稿' }));

    await waitFor(() =>
      expect(chatApi.applyRevision).toHaveBeenCalledWith(mockArticle.url_hash, 0, 'rev-001'),
    );
  });
});
