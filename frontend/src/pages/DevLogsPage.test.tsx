import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import dayjs from 'dayjs';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { devLogsApi } from '../api/client';
import type { DevLogEntry, DevLogQueryResult, DevLogStats, DevLogTrace } from '../types';
import DevLogsPage from './DevLogsPage';

vi.mock('../api/client', () => ({
  devLogsApi: {
    query: vi.fn(),
    dates: vi.fn(),
    trace: vi.fn(),
    stats: vi.fn(),
  },
}));

const errorLog: DevLogEntry = {
  _id: 'mongo-log-1',
  log_id: 'log-1',
  trace_id: 'trace-1',
  user_id: 'user-a',
  username: 'alice',
  level: 'ERROR',
  phase: 'draft',
  action: 'error',
  message: '草稿生成失败',
  detail: { draft_count: 0 },
  duration_ms: 321,
  error: {
    type: 'RuntimeError',
    message: 'model unavailable',
    stack_trace: 'Traceback: RuntimeError',
  },
  created_at: '2026-07-13T10:00:00+08:00',
  date: '2026-07-13',
};

const queryResult: DevLogQueryResult = {
  logs: [errorLog],
  phases: ['crawl', 'draft'],
  levels: ['INFO', 'ERROR'],
  users: [{ user_id: 'user-a', username: 'alice' }],
  total: 1,
  page: 1,
  page_size: 50,
};

const stats: DevLogStats = {
  total: 1,
  by_level: { ERROR: 1 },
  by_phase: { draft: 1 },
  by_user: [{ user_id: 'user-a', username: 'alice', count: 1 }],
  error_count: 1,
  avg_duration_ms: { draft: 321 },
};

const trace: DevLogTrace = {
  trace_id: 'trace-1',
  user_id: 'user-a',
  username: 'alice',
  events: [errorLog],
  total_duration_ms: 321,
  phase_count: 1,
  has_error: true,
};

describe('DevLogsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(devLogsApi.query).mockResolvedValue(queryResult);
    vi.mocked(devLogsApi.dates).mockResolvedValue(['2026-07-13', '2026-07-12']);
    vi.mocked(devLogsApi.stats).mockResolvedValue(stats);
    vi.mocked(devLogsApi.trace).mockResolvedValue(trace);
  });

  it('renders all filters and loads the current date', async () => {
    render(<DevLogsPage />);

    expect(screen.getByText('开发者日志')).toBeInTheDocument();
    expect(screen.getByLabelText('日期')).toBeInTheDocument();
    expect(screen.getByText('用户', { selector: 'label' })).toBeInTheDocument();
    expect(screen.getByText('阶段', { selector: 'label' })).toBeInTheDocument();
    expect(screen.getByText('级别', { selector: 'label' })).toBeInTheDocument();
    expect(screen.getByPlaceholderText('输入链路 ID')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('搜索消息')).toBeInTheDocument();

    await waitFor(() => {
      expect(devLogsApi.query).toHaveBeenCalledWith({
        date: dayjs().format('YYYY-MM-DD'),
        page: 1,
        page_size: 50,
      });
    });
    expect(screen.getByText('草稿生成失败')).toBeInTheDocument();
  });

  it('submits a keyword filter and resets pagination', async () => {
    render(<DevLogsPage />);
    await waitFor(() => expect(devLogsApi.query).toHaveBeenCalledTimes(1));
    vi.mocked(devLogsApi.query).mockClear();

    fireEvent.change(screen.getByPlaceholderText('搜索消息'), {
      target: { value: '草稿' },
    });
    fireEvent.click(screen.getByRole('button', { name: /查询/ }));

    await waitFor(() => {
      expect(devLogsApi.query).toHaveBeenCalledWith(
        expect.objectContaining({
          date: dayjs().format('YYYY-MM-DD'),
          keyword: '草稿',
          page: 1,
          page_size: 50,
        }),
      );
    });
  });

  it('expands a log row to show detail and error JSON', async () => {
    render(<DevLogsPage />);
    const message = await screen.findByText('草稿生成失败');
    const row = message.closest('tr');
    expect(row).not.toBeNull();

    fireEvent.click(row as HTMLElement);

    await waitFor(() => {
      expect(screen.getByText(/"draft_count": 0/)).toBeInTheDocument();
    });
    expect(screen.getByText(/"stack_trace": "Traceback: RuntimeError"/)).toBeInTheDocument();
  });

  it('opens a trace timeline from the trace id', async () => {
    render(<DevLogsPage />);
    const traceButton = await screen.findByRole('button', { name: 'trace-1' });

    fireEvent.click(traceButton);

    await waitFor(() => expect(devLogsApi.trace).toHaveBeenCalledWith('trace-1'));
    expect(await screen.findByText('Trace 链路：trace-1')).toBeInTheDocument();
    expect(screen.getByText('存在错误')).toBeInTheDocument();
  });
});
