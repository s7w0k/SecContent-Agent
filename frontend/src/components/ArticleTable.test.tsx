import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { Article } from '../types';
import ArticleTable from './ArticleTable';

const mockArticle: Article = {
  _id: '1',
  url_hash: 'abc123',
  title: 'Critical MCP Vulnerability Found',
  url: 'https://example.com/mcp',
  source: 'The Hacker News',
  source_type: 'overseas_news',
  published_at: '2026-06-29',
  added_at: '2026-06-29T12:00:00',
  summary: 'A critical vulnerability...',
  summary_cn: 'MCP严重漏洞',
  is_ai_security: true,
  is_agent_security: true,
  category: 'MCP协议漏洞',
  category_v2: '爆点事件',
  product_relevance: 92,
  event_impact: 78,
  pr_total_score: 170,
  ai_relevance_score: 92,
  reportability_score: 78,
  total_score: 170,
  is_high_value: true,
  has_report: true,
  report_id: 'rpt-1',
};

describe('ArticleTable', () => {
  const defaultProps = {
    articles: [mockArticle],
    total: 1,
    loading: false,
    page: 1,
    pageSize: 20,
    onPageChange: vi.fn(),
    onSortChange: vi.fn(),
    onViewReport: vi.fn(),
    onViewDetail: vi.fn(),
    onViewDrafts: vi.fn(),
    onRunV2Single: vi.fn(),
    onScoreV2Single: vi.fn(),
    onRefresh: vi.fn(),
  };

  it('renders article title as link', () => {
    render(<ArticleTable {...defaultProps} />);
    const link = screen.getByText('Critical MCP Vulnerability Found');
    expect(link).toBeDefined();
  });

  it('renders category tag', () => {
    render(<ArticleTable {...defaultProps} />);
    expect(screen.getByText('爆点事件')).toBeDefined();
  });

  it('renders score values', () => {
    render(<ArticleTable {...defaultProps} />);
    expect(screen.getByText('92')).toBeDefined();
    expect(screen.getByText('78')).toBeDefined();
    expect(screen.getByText('170')).toBeDefined();
  });

  it('shows report button when has_report is true', () => {
    render(<ArticleTable {...defaultProps} />);
    expect(screen.getByText('报道')).toBeDefined();
  });

  it('shows detail button', () => {
    render(<ArticleTable {...defaultProps} />);
    expect(screen.getByText('详情')).toBeDefined();
  });

  it('shows empty state when no articles', () => {
    render(<ArticleTable {...defaultProps} articles={[]} total={0} />);
    expect(screen.getByText('no data')).toBeDefined();
  });

  it('renders a purple user upload source tag', () => {
    render(
      <ArticleTable
        {...defaultProps}
        articles={[
          {
            ...mockArticle,
            _id: 'upload-1',
            url: 'upload://user-a/article.md',
            source: '用户上传',
            source_type: 'user_upload',
          },
        ]}
      />,
    );

    const tag = screen.getByText('用户上传');
    expect(tag).toHaveClass('ant-tag-purple');
  });
});
