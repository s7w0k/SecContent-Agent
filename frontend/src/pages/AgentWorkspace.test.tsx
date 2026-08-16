import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentRun } from '../types';
import AgentWorkspace from './AgentWorkspace';

const api = vi.hoisted(() => ({
  listRuns: vi.fn(),
  getRun: vi.fn(),
  submitTurn: vi.fn(),
  cancelRun: vi.fn(),
  approveRun: vi.fn(),
  openEventSource: vi.fn(),
}));

vi.mock('../api/client', () => ({ agentApi: api }));

function run(overrides: Partial<AgentRun> = {}): AgentRun {
  return {
    run_id: 'run-1',
    task_id: 'task-1',
    thread_id: 'thread-1',
    status: 'waiting_user',
    intent: 'search_and_rank',
    changed_slots: [],
    invalidated_steps: [],
    questions: [{ slot: 'selected_article_ids', question: '请选择一篇新闻' }],
    assumptions: [],
    result: {
      items: [{ article_id: 'a1', title: '安全新闻', source: '测试源', score: 0.9 }],
    },
    error: '',
    created_at: '2026-08-16T00:00:00Z',
    updated_at: '2026-08-16T00:00:01Z',
    ...overrides,
  };
}

describe('AgentWorkspace', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.openEventSource.mockReturnValue({ close: vi.fn() });
    api.submitTurn.mockImplementation(async () => ({
      duplicate: false,
      task: {},
      run: run({ status: 'running', questions: [] }),
    }));
  });

  it('restores history and submits a structured candidate choice', async () => {
    api.listRuns.mockResolvedValue([run()]);
    render(<AgentWorkspace />);
    fireEvent.click(await screen.findByText('搜索并挑选新闻'));
    expect(await screen.findByText('安全新闻')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('radio', { name: '选择第 1 条' }));
    await waitFor(() =>
      expect(api.submitTurn).toHaveBeenCalledWith(
        expect.objectContaining({ content: '安全新闻', thread_id: 'thread-1' }),
      ),
    );
  });

  it('submits a natural-language answer and renders final result', async () => {
    const completed = run({
      run_id: 'run-2',
      status: 'completed',
      intent: 'search_and_draft',
      questions: [],
      result: {
        items: [{ article_id: 'a1', title: '安全新闻', source: '测试源', score: 0.9 }],
        message: '检索到 1 条候选',
      },
    });
    api.listRuns.mockResolvedValue([run(), completed]);
    render(<AgentWorkspace />);
    fireEvent.click(await screen.findByText('搜索并挑选新闻'));
    fireEvent.change(screen.getByPlaceholderText('选择候选或继续描述需求'), { target: { value: '第二条' } });
    fireEvent.click(screen.getByRole('button', { name: /回答/ }));
    await waitFor(() =>
      expect(api.submitTurn).toHaveBeenCalledWith(
        expect.objectContaining({ content: '第二条' }),
      ),
    );

    fireEvent.click(await screen.findByText('搜索新闻并写稿'));
    expect(await screen.findByText('检索到 1 条候选')).toBeInTheDocument();
    expect(screen.getByText('安全新闻')).toBeInTheDocument();
  });

  it('submits a new goal through the conversation entry', async () => {
    api.listRuns.mockResolvedValue([]);
    render(<AgentWorkspace />);
    fireEvent.change(screen.getByPlaceholderText(/描述任务目标/), { target: { value: '搜索智能体安全新闻并写稿' } });
    fireEvent.click(screen.getByRole('button', { name: /发送/ }));
    await waitFor(() =>
      expect(api.submitTurn).toHaveBeenCalledWith({ content: '搜索智能体安全新闻并写稿' }),
    );
  });
});
