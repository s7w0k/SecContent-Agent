import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { StatsData } from '../types';
import TodayStatsRow from './TodayStatsRow';

const stats: StatsData = {
  total_articles: 150,
  ai_security_count: 45,
  high_value_count: 12,
  source_distribution: {},
  category_distribution: {},
  today_count: 8,
  today_ai_security_count: 5,
  today_high_value_count: 2,
};

describe('TodayStatsRow', () => {
  it('renders the local date and today values', () => {
    render(<TodayStatsRow stats={stats} loading={false} />);

    expect(
      screen.getByText(`今日新增 (${new Date().toLocaleDateString('zh-CN')})`),
    ).toBeInTheDocument();
    expect(screen.getByText('今日收录')).toBeInTheDocument();
    expect(screen.getByText('今日 AI 安全')).toBeInTheDocument();
    expect(screen.getByText('今日高价值')).toBeInTheDocument();
    expect(screen.getByText('8')).toBeInTheDocument();
    expect(screen.getByText('5')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
  });

  it('renders a skeleton in each card while loading', () => {
    const { container } = render(<TodayStatsRow stats={null} loading />);

    expect(container.querySelectorAll('.ant-skeleton')).toHaveLength(3);
  });

  it('renders zero values when stats are unavailable', () => {
    render(<TodayStatsRow stats={null} loading={false} />);

    expect(screen.getAllByText('0')).toHaveLength(3);
  });
});
