import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import PipelineControl from './PipelineControl';

const apiMock = vi.hoisted(() => ({
  run: vi.fn(),
  crawl: vi.fn(),
  crawlOverseas: vi.fn(),
  crawlWewe: vi.fn(),
  score: vi.fn(),
  scoreV2: vi.fn(),
  classifyV2: vi.fn(),
  runV2: vi.fn(),
  report: vi.fn(),
  getStatus: vi.fn().mockResolvedValue({
    status: 'idle',
    current_phase: '',
    state: {},
    errors: [],
  }),
}));

vi.mock('../api/client', () => ({
  default: apiMock,
}));

describe('PipelineControl', () => {
  const defaultProps = {
    onComplete: vi.fn(),
    onRefresh: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders trigger buttons', () => {
    render(<PipelineControl {...defaultProps} />);
    expect(screen.getByText('全流程')).toBeDefined();
    expect(screen.getByText('爬取+分类')).toBeDefined();
    expect(screen.getByText('V2打分')).toBeDefined();
    expect(screen.getByText('仅报道')).toBeDefined();
  });

  it('shows idle status initially', () => {
    render(<PipelineControl {...defaultProps} />);
    expect(screen.getByText(/空闲/)).toBeDefined();
  });

  it('shows hint when no state', () => {
    render(<PipelineControl {...defaultProps} />);
    expect(screen.getByText(/点击.*全流程.*开始/)).toBeDefined();
  });

  it('shows pipeline control title', () => {
    render(<PipelineControl {...defaultProps} />);
    expect(screen.getByText('流水线控制')).toBeDefined();
  });

  it('shows live progress immediately while a synchronous task is running', () => {
    apiMock.crawlOverseas.mockReturnValue(new Promise(() => undefined));
    render(<PipelineControl {...defaultProps} />);

    fireEvent.click(screen.getByRole('button', { name: '海外新闻' }));

    expect(screen.getByLabelText('海外新闻执行进度')).toBeDefined();
    expect(screen.getByText('正在连接海外新闻服务并抓取、解析、保存文章...')).toBeDefined();
    expect(screen.getByText(/已耗时/)).toBeDefined();
  });
});
