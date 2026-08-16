import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { AgentCandidate } from '../types';
import AgentCandidateCards from './AgentCandidateCards';

const candidates: AgentCandidate[] = [
  {
    article_id: 'a-1',
    title: 'AI 安全新规发布',
    source: '来源甲',
    published_at: '2026-08-16T08:00:00Z',
    summary: '监管机构发布新的安全要求。',
    score: 0.92,
  },
  {
    article_id: 'a-2',
    title: 'AI 安全产品更新',
    source: '来源乙',
    summary: '厂商披露产品更新。',
    score: 0.73,
  },
];

describe('AgentCandidateCards', () => {
  it('renders an explicit empty state', () => {
    render(<AgentCandidateCards candidates={[]} onSelect={vi.fn()} />);
    expect(screen.getByText('没有找到匹配的新闻')).toBeInTheDocument();
  });

  it('renders title, source, date and invokes selection', () => {
    const onSelect = vi.fn();
    render(<AgentCandidateCards candidates={candidates} onSelect={onSelect} />);
    expect(screen.getByText('AI 安全新规发布')).toBeInTheDocument();
    expect(screen.getByText(/来源甲 · 2026-08-16/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('radio', { name: '选择第 2 条' }));
    expect(onSelect).toHaveBeenCalledWith(candidates[1]);
  });

  it('keeps the selected candidate visible', () => {
    render(<AgentCandidateCards candidates={candidates} selectedId="a-1" onSelect={vi.fn()} />);
    expect(screen.getByText('已选择')).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: '选择第 1 条' })).toBeChecked();
  });
});
