import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import api from '../api/client';
import Dashboard from './Dashboard';

// Mock all child components and API
vi.mock('../api/client', () => ({
  default: {
    getStats: vi.fn().mockResolvedValue({
      total_articles: 100,
      ai_security_count: 30,
      high_value_count: 10,
      source_distribution: {},
      category_distribution: { MCP协议漏洞: 5 },
      today_count: 3,
      today_ai_security_count: 2,
      today_high_value_count: 1,
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
    getStatus: vi.fn().mockResolvedValue({ status: 'idle', state: {}, errors: [] }),
    run: vi.fn(),
    crawl: vi.fn(),
    score: vi.fn(),
    report: vi.fn(),
  },
}));

vi.mock('../components/StatsCards', () => ({
  default: () => <div>StatsCards Mock</div>,
}));
vi.mock('../components/TodayStatsRow', () => ({
  default: ({ stats }: { stats: { today_count: number } | null }) => (
    <div>TodayStatsRow Mock {stats?.today_count ?? 'loading'}</div>
  ),
}));
vi.mock('../components/CategoryBreakdown', () => ({
  default: ({
    distribution,
    onCategoryClick,
  }: {
    distribution: Record<string, number>;
    onCategoryClick?: (category: string) => void;
  }) => (
    <button type="button" onClick={() => onCategoryClick?.('MCP协议漏洞')}>
      CategoryBreakdown Mock {distribution.MCP协议漏洞 ?? 'loading'}
    </button>
  ),
}));
vi.mock('../components/HotRankingPanel', () => ({
  default: () => <div>HotRankingPanel Mock</div>,
}));
vi.mock('../components/FilterBar', () => ({
  default: () => <div>FilterBar Mock</div>,
}));
vi.mock('../components/ArticleTable', () => ({
  default: () => <div>ArticleTable Mock</div>,
}));
vi.mock('../components/ArticleUpload', () => ({
  default: ({
    open,
    onUploaded,
  }: {
    open: boolean;
    onUploaded: () => void | Promise<void>;
  }) =>
    open ? (
      <button type="button" onClick={() => onUploaded()}>
        ArticleUpload Mock
      </button>
    ) : null,
}));
vi.mock('../components/PipelineControl', () => ({
  default: () => <div>PipelineControl Mock</div>,
}));
vi.mock('../components/ReportViewer', () => ({
  default: () => <div>ReportViewer Mock</div>,
}));

describe('Dashboard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders page title', async () => {
    render(<Dashboard />);
    expect(screen.getByText('🚀 PR Agent Dashboard')).toBeDefined();
    expect(await screen.findByText('TodayStatsRow Mock 3')).toBeDefined();
  });

  it('renders all dashboard components and passes shared stats to analytics', async () => {
    render(<Dashboard />);
    expect(screen.getByText('StatsCards Mock')).toBeDefined();
    expect(await screen.findByText('TodayStatsRow Mock 3')).toBeDefined();
    expect(screen.getByText('CategoryBreakdown Mock 5')).toBeDefined();
    expect(screen.getByText('HotRankingPanel Mock')).toBeDefined();
    expect(screen.getByText('FilterBar Mock')).toBeDefined();
    expect(screen.getByText('ArticleTable Mock')).toBeDefined();
    expect(screen.getByText('PipelineControl Mock')).toBeDefined();
    expect(screen.queryByText('API 抓取配置')).not.toBeInTheDocument();
  });

  it('uses a responsive 16/8 desktop layout and full-width small-screen columns', async () => {
    render(<Dashboard />);
    expect(await screen.findByText('TodayStatsRow Mock 3')).toBeDefined();

    expect(screen.getByTestId('article-table-column')).toHaveClass(
      'ant-col-xs-24',
      'ant-col-sm-24',
      'ant-col-lg-16',
    );
    expect(screen.getByTestId('hot-ranking-column')).toHaveClass(
      'ant-col-xs-24',
      'ant-col-sm-24',
      'ant-col-lg-8',
    );
  });

  it('filters the article table when a category is clicked', async () => {
    render(<Dashboard />);

    fireEvent.click(await screen.findByRole('button', { name: 'CategoryBreakdown Mock 5' }));

    await waitFor(() =>
      expect(api.getArticles).toHaveBeenCalledWith(
        expect.objectContaining({ category: 'MCP协议漏洞', page: 1 }),
      ),
    );
  });

  it('loads shared stats and the initial article page without duplicate requests', async () => {
    render(<Dashboard />);

    await waitFor(() => expect(api.getStats).toHaveBeenCalledTimes(1));
    expect(api.getArticles).toHaveBeenCalledTimes(1);
  });

  it('opens the upload modal and refreshes articles and stats after upload', async () => {
    render(<Dashboard />);
    await waitFor(() => expect(api.getStats).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: /上传文章/ }));
    fireEvent.click(screen.getByRole('button', { name: 'ArticleUpload Mock' }));

    await waitFor(() => expect(api.getStats).toHaveBeenCalledTimes(2));
    expect(api.getArticles).toHaveBeenCalledTimes(2);
  });
});
