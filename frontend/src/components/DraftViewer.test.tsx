import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { Article } from '../types';
import DraftViewer from './DraftViewer';

vi.mock('../api/client', () => ({
  default: {
    create: vi.fn(),
    log: vi.fn().mockResolvedValue({}),
  },
}));

vi.mock('react-markdown', () => ({
  default: ({ children }: { children: string }) => <div>{children}</div>,
}));

const article = {
  _id: '1',
  url_hash: 'd41d8cd98f00b204e9800998ecf8427e',
  title: '测试文章',
  category_v2: '爆点事件',
  product_relevance: 80,
  event_impact: 70,
  pr_total_score: 150,
  pr_drafts: [
    {
      template: '爆点A',
      perspective: '产品能力视角',
      content_md: '稿件正文',
      title: '测试文章',
      index: 1,
      review: {
        status: 'completed',
        content_hash: '0'.repeat(64),
        summary: '发现 1 个建议修改问题',
        issues: [
          {
            issue_id: 'issue-001',
            category: 'absolute_claim',
            severity: 'medium',
            quote: '业内第一',
            reason: '缺少排名依据',
            suggestion: '删除绝对化表达',
            suggested_rewrite: '持续提升产品能力',
          },
        ],
        counts: { high: 0, medium: 1, low: 0 },
        fact_check_available: true,
        reviewed_at: '2026-07-22T08:00:00Z',
      },
    },
  ],
} as Article;

describe('DraftViewer', () => {
  it('shows the current draft review result', async () => {
    render(<DraftViewer article={article} onClose={vi.fn()} />);

    expect(await screen.findByText('内容与话术检查')).toBeInTheDocument();
    expect(screen.getByText('建议修改 1')).toBeInTheDocument();
    expect(screen.getByText('缺少排名依据')).toBeInTheDocument();
  });
});
