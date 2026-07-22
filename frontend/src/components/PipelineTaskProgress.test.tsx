import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import PipelineTaskProgress from './PipelineTaskProgress';

const getTaskStatus = vi.hoisted(() => vi.fn());

vi.mock('../api/client', () => ({
  pipelineApi: { getTaskStatus },
}));

describe('PipelineTaskProgress', () => {
  it('starts a newly created task at zero instead of applying a timezone offset', async () => {
    getTaskStatus.mockResolvedValue({
      task_id: 'new-task',
      user_id: 'user-1',
      task_type: 'run-v2',
      status: 'running',
      progress: { phase: 'draft', current: 0, total: 4, message: '正在生成草稿...' },
      // 即使历史响应不带时区，也应优先使用后端的 elapsed_seconds。
      created_at: '2026-07-22T08:00:00',
      updated_at: '2026-07-22T08:00:00',
      expires_at: '2026-07-22T09:00:00',
      elapsed_seconds: 0,
    });

    render(<PipelineTaskProgress taskId="new-task" label="草稿生成" onCompleted={vi.fn()} />);

    await waitFor(() => expect(screen.getByText(/已耗时 0分00秒/)).toBeDefined());
  });

  it('uses and freezes the persisted elapsed time after completion', async () => {
    getTaskStatus.mockResolvedValue({
      task_id: 'completed-task',
      user_id: 'user-1',
      task_type: 'run-v2',
      status: 'completed',
      progress: { phase: 'completed', current: 4, total: 4, message: '任务完成' },
      created_at: '2026-07-22T08:00:00Z',
      updated_at: '2026-07-22T08:02:05Z',
      expires_at: '2026-07-22T09:00:00Z',
      elapsed_seconds: 125,
    });

    render(<PipelineTaskProgress taskId="completed-task" label="草稿生成" onCompleted={vi.fn()} />);

    await waitFor(() => expect(screen.getByText(/已耗时 2分05秒/)).toBeDefined());
  });

  it('renders persisted task phase and real percentage', async () => {
    getTaskStatus.mockResolvedValue({
      task_id: 'task-1',
      user_id: 'user-1',
      task_type: 'run-v2',
      status: 'running',
      progress: {
        phase: 'score',
        current: 3,
        total: 8,
        message: '正在评估文章...',
      },
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      expires_at: new Date().toISOString(),
    });

    render(<PipelineTaskProgress taskId="task-1" label="智能PR流水线" onCompleted={vi.fn()} />);

    await waitFor(() => expect(screen.getByText('内容打分')).toBeDefined());
    expect(screen.getByText('正在评估文章...')).toBeDefined();
    expect(screen.getByText('步骤 4 / 8')).toBeDefined();
    expect(screen.getByText('38%')).toBeDefined();
  });

  it('shows completed, remaining and total article counts for V2 batch tasks', async () => {
    getTaskStatus.mockResolvedValue({
      task_id: 'classify-task',
      user_id: 'user-1',
      task_type: 'classify-v2',
      status: 'running',
      progress: {
        phase: 'classify_v2',
        current: 6,
        total: 10,
        message: '已分类 6 篇，剩余 4 篇',
      },
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      expires_at: new Date().toISOString(),
    });

    render(<PipelineTaskProgress taskId="classify-task" label="V2分类" onCompleted={vi.fn()} />);

    await waitFor(() => expect(screen.getByText('已分类 6 篇，剩余 4 篇')).toBeDefined());
    expect(screen.getByText('已完成 6 篇 · 剩余 4 篇 · 共 10 篇')).toBeDefined();
    expect(screen.getByText('60%')).toBeDefined();
  });
});
