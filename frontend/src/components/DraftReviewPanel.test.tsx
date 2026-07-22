import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { DraftReview } from '../types';
import DraftReviewPanel from './DraftReviewPanel';

const review: DraftReview = {
  status: 'completed',
  content_hash: '0'.repeat(64),
  summary: '发现 1 个必须修改问题、1 个建议修改问题、1 个表达优化问题',
  fact_check_available: true,
  counts: { high: 1, medium: 1, low: 1 },
  reviewed_at: '2026-07-22T08:00:00Z',
  issues: [
    {
      issue_id: 'issue-001',
      category: 'fact_mismatch',
      severity: 'high',
      quote: '漏洞已经影响全部用户。',
      reason: '原文仅表示可能影响部分用户',
      suggestion: '恢复原文的不确定性和影响范围',
      suggested_rewrite: '漏洞可能影响部分用户。',
    },
    {
      issue_id: 'issue-002',
      category: 'absolute_claim',
      severity: 'medium',
      quote: '我们是业内第一。',
      reason: '缺少排名范围和依据',
      suggestion: '删除第一，描述具体能力',
      suggested_rewrite: '我们持续提升相关产品能力。',
    },
    {
      issue_id: 'issue-003',
      category: 'ambiguous_expression',
      severity: 'low',
      quote: '能力实现全面提升。',
      reason: '提升范围不明确',
      suggestion: '说明具体提升内容',
    },
  ],
};

describe('DraftReviewPanel', () => {
  beforeEach(() => {
    vi.stubGlobal('crypto', {
      subtle: {
        digest: vi.fn().mockResolvedValue(new Uint8Array(32).buffer),
      },
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('shows severity counts, issue details and grouped filters', () => {
    render(<DraftReviewPanel review={review} contentMd="稿件正文" />);

    expect(screen.getByText('必须修改 1')).toBeInTheDocument();
    expect(screen.getByText('建议修改 1')).toBeInTheDocument();
    expect(screen.getByText('表达优化 1')).toBeInTheDocument();
    expect(screen.getByText('与原文不一致')).toBeInTheDocument();
    expect(screen.getByText('绝对化表达')).toBeInTheDocument();

    fireEvent.click(screen.getByText('事实内容 (1)'));
    expect(screen.getByText('漏洞已经影响全部用户。')).toBeInTheDocument();
    expect(screen.queryByText('我们是业内第一。')).not.toBeInTheDocument();

    fireEvent.click(screen.getByText('宣传话术 (2)'));
    expect(screen.getByText('我们是业内第一。')).toBeInTheDocument();
    expect(screen.queryByText('漏洞已经影响全部用户。')).not.toBeInTheDocument();
  });

  it('copies a suggested rewrite', async () => {
    render(<DraftReviewPanel review={review} contentMd="稿件正文" />);

    fireEvent.click(screen.getByRole('button', { name: '复制推荐改写：我们是业内第一。' }));
    await waitFor(() =>
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith('我们持续提升相关产品能力。'),
    );
  });

  it('shows stale, failed, partial, loading and empty states', async () => {
    const { rerender } = render(
      <DraftReviewPanel
        review={{ ...review, content_hash: 'f'.repeat(64) }}
        contentMd="已修改正文"
      />,
    );
    await screen.findByText('稿件内容已修改，请重新检查');

    rerender(
      <DraftReviewPanel
        review={{
          ...review,
          status: 'failed',
          issues: [],
          counts: { high: 0, medium: 0, low: 0 },
          error: '模型超时',
        }}
        contentMd="稿件正文"
      />,
    );
    expect(screen.getByText('稿件检查失败')).toBeInTheDocument();
    expect(screen.getByText('模型超时')).toBeInTheDocument();

    rerender(
      <DraftReviewPanel
        review={{ ...review, status: 'partial', summary: '事实检查不完整：缺少原文内容' }}
        contentMd="稿件正文"
      />,
    );
    expect(screen.getByText('检查部分完成')).toBeInTheDocument();

    rerender(<DraftReviewPanel contentMd="稿件正文" reviewing />);
    expect(screen.getByText('正在检查稿件内容与宣传话术...')).toBeInTheDocument();

    rerender(<DraftReviewPanel contentMd="稿件正文" />);
    expect(screen.getByText('尚未检查这篇稿件')).toBeInTheDocument();
  });

  it('triggers a manual review', () => {
    const onReview = vi.fn();
    render(<DraftReviewPanel contentMd="稿件正文" onReview={onReview} />);

    fireEvent.click(screen.getByRole('button', { name: /开始检查/ }));
    expect(onReview).toHaveBeenCalledOnce();
  });
});
