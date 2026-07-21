import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import CategoryBreakdown from './CategoryBreakdown';

describe('CategoryBreakdown', () => {
  it('sorts categories and renders count, share, and normalized progress', () => {
    const { container } = render(
      <CategoryBreakdown distribution={{ 小分类: 10, 最大分类: 60, 中分类: 30 }} loading={false} />,
    );

    const categoryButtons = screen.getAllByRole('button', { pressed: false });
    expect(categoryButtons.map((button) => button.textContent)).toEqual([
      '最大分类60 (60.0%)',
      '中分类30 (30.0%)',
      '小分类10 (10.0%)',
    ]);
    const progressBars = container.querySelectorAll('.ant-progress-bg');
    expect(progressBars[0]).toHaveStyle({ width: '100%' });
    expect(progressBars[1]).toHaveStyle({ width: '50%' });
  });

  it('calls onCategoryClick and highlights the selected row', () => {
    const onCategoryClick = vi.fn();
    render(
      <CategoryBreakdown
        distribution={{ AI安全漏洞与攻击: 8, AI合规与治理: 2 }}
        loading={false}
        onCategoryClick={onCategoryClick}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /AI安全漏洞与攻击/ }));

    expect(onCategoryClick).toHaveBeenCalledWith('AI安全漏洞与攻击');
    expect(screen.getByRole('button', { name: /AI安全漏洞与攻击/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });

  it('renders skeletons while loading', () => {
    const { container } = render(<CategoryBreakdown distribution={{}} loading />);

    expect(container.querySelectorAll('.ant-skeleton')).toHaveLength(4);
  });

  it('shows six categories by default and toggles the full list when over eight', () => {
    const distribution = Object.fromEntries(
      Array.from({ length: 9 }, (_, index) => [`分类${index + 1}`, 9 - index]),
    );
    render(<CategoryBreakdown distribution={distribution} loading={false} />);

    expect(screen.getAllByRole('button', { pressed: false })).toHaveLength(6);
    expect(screen.queryByText('分类7')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /展开全部/ }));

    expect(screen.getAllByRole('button', { pressed: false })).toHaveLength(9);
    expect(screen.getByText('分类9')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /收起/ }));
    expect(screen.getAllByRole('button', { pressed: false })).toHaveLength(6);
  });

  it('can collapse and expand the whole panel', () => {
    render(<CategoryBreakdown distribution={{ 分类A: 1 }} loading={false} />);

    fireEvent.click(screen.getByRole('button', { name: /分类分布/ }));
    expect(screen.getByRole('button', { name: /分类分布/ })).toHaveAttribute(
      'aria-expanded',
      'false',
    );

    fireEvent.click(screen.getByRole('button', { name: /分类分布/ }));
    expect(screen.getByRole('button', { name: /分类分布/ })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
  });
});
