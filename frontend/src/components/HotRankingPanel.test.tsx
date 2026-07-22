import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { HotArticle } from '../types';
import HotRankingPanel from './HotRankingPanel';

const { getHotRankingMock } = vi.hoisted(() => ({
  getHotRankingMock: vi.fn(),
}));

vi.mock('../api/client', () => ({
  default: { getHotRanking: getHotRankingMock },
}));

const articles: HotArticle[] = [
  {
    url_hash: 'hot-1',
    title: '热点文章一',
    url: 'https://example.com/hot-1',
    pr_total_score: 188,
    category_v2: '爆点事件',
    added_at: '2026-07-21T08:30:00Z',
    source_type: 'overseas_news',
  },
  {
    url_hash: 'hot-2',
    title: '热点文章二',
    url: 'https://example.com/hot-2',
    pr_total_score: 176,
    category_v2: 'AI技术重大进展',
    added_at: '2026-07-20T08:30:00Z',
    source_type: 'wechat_mp',
  },
  {
    url_hash: 'hot-3',
    title: '热点文章三',
    url: 'https://example.com/hot-3',
    pr_total_score: 165,
    category_v2: '学术/会展/高校',
    added_at: '2026-07-19T08:30:00Z',
    source_type: 'paper',
  },
];

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe('HotRankingPanel', () => {
  beforeEach(() => {
    getHotRankingMock.mockReset();
  });

  it('renders the guided empty state', async () => {
    getHotRankingMock.mockResolvedValue([]);
    render(<HotRankingPanel />);

    expect(await screen.findByText('暂无高价值文章，可尝试扩大时间范围或分类')).toBeInTheDocument();
    expect(getHotRankingMock).toHaveBeenCalledWith({
      limit: 10,
      category: 'all',
      date_range: '7d',
    });
  });

  it('renders ranked articles, medals, scores, categories, and external links', async () => {
    getHotRankingMock.mockResolvedValue(articles);
    render(<HotRankingPanel />);

    const firstLink = await screen.findByRole('link', { name: '热点文章一' });
    expect(firstLink).toHaveAttribute('href', 'https://example.com/hot-1');
    expect(firstLink).toHaveAttribute('target', '_blank');
    expect(screen.getByText('🥇')).toBeInTheDocument();
    expect(screen.getByText('🥈')).toBeInTheDocument();
    expect(screen.getByText('🥉')).toBeInTheDocument();
    expect(screen.getByText('188 分')).toBeInTheDocument();
    expect(screen.getByText('爆点事件')).toBeInTheDocument();
  });

  it('reloads when the category or date range changes', async () => {
    getHotRankingMock.mockResolvedValue([]);
    render(<HotRankingPanel />);
    await waitFor(() => expect(getHotRankingMock).toHaveBeenCalledTimes(1));

    fireEvent.mouseDown(screen.getByRole('combobox', { name: '热点分类' }));
    const categoryOption = Array.from(document.querySelectorAll('.ant-select-item-option')).find(
      (option) => option.textContent === '爆点事件',
    );
    expect(categoryOption).toBeDefined();
    fireEvent.click(categoryOption as Element);
    await waitFor(() =>
      expect(getHotRankingMock).toHaveBeenLastCalledWith({
        limit: 10,
        category: '爆点事件',
        date_range: '7d',
      }),
    );

    fireEvent.click(screen.getByLabelText('今日'));
    await waitFor(() =>
      expect(getHotRankingMock).toHaveBeenLastCalledWith({
        limit: 10,
        category: '爆点事件',
        date_range: '1d',
      }),
    );
  });

  it('reloads when refresh is clicked', async () => {
    getHotRankingMock.mockResolvedValue([]);
    render(<HotRankingPanel />);
    await waitFor(() => expect(getHotRankingMock).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: '刷新热点排行' }));

    await waitFor(() => expect(getHotRankingMock).toHaveBeenCalledTimes(2));
  });

  it('shows the loading indicator while the request is pending', () => {
    getHotRankingMock.mockReturnValue(new Promise(() => undefined));
    const { container } = render(<HotRankingPanel />);

    expect(container.querySelector('.ant-spin-spinning')).toBeInTheDocument();
  });

  it('distinguishes a request failure from a real empty result', async () => {
    getHotRankingMock.mockRejectedValue(new Error('network unavailable'));
    render(<HotRankingPanel />);

    expect(await screen.findByText('热点排行加载失败')).toBeInTheDocument();
    expect(screen.getByText('热点排行加载失败，请检查网络或稍后重试')).toBeInTheDocument();
    expect(screen.queryByText('暂无高价值文章，可尝试扩大时间范围或分类')).not.toBeInTheDocument();
  });

  it('recovers from an error when retry succeeds', async () => {
    getHotRankingMock
      .mockRejectedValueOnce(new Error('temporary failure'))
      .mockResolvedValueOnce([articles[0]]);
    render(<HotRankingPanel />);

    await screen.findByText('热点排行加载失败');
    fireEvent.click(screen.getByRole('button', { name: '重试热点排行' }));

    expect(await screen.findByRole('link', { name: '热点文章一' })).toBeInTheDocument();
    expect(screen.queryByText('热点排行加载失败')).not.toBeInTheDocument();
  });

  it('keeps the newest result during rapid filter changes', async () => {
    const first = deferred<HotArticle[]>();
    const second = deferred<HotArticle[]>();
    const third = deferred<HotArticle[]>();
    getHotRankingMock
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
      .mockReturnValueOnce(third.promise);
    render(<HotRankingPanel />);
    await waitFor(() => expect(getHotRankingMock).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByLabelText('今日'));
    await waitFor(() => expect(getHotRankingMock).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByLabelText('近30天'));
    await waitFor(() => expect(getHotRankingMock).toHaveBeenCalledTimes(3));

    await act(async () => third.resolve([articles[2]]));
    expect(await screen.findByRole('link', { name: '热点文章三' })).toBeInTheDocument();

    await act(async () => {
      first.resolve([articles[0]]);
      second.resolve([articles[1]]);
    });
    expect(screen.getByRole('link', { name: '热点文章三' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: '热点文章一' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: '热点文章二' })).not.toBeInTheDocument();
  });
});
