import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import PipelineTaskProgress from './PipelineTaskProgress';

const getTaskStatus = vi.hoisted(() => vi.fn());

vi.mock('../api/client', () => ({
  pipelineApi: { getTaskStatus },
}));

describe('PipelineTaskProgress', () => {
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
