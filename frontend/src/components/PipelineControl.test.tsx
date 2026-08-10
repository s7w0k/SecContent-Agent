import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { RuntimeSummary } from '../types';
import PipelineControl from './PipelineControl';

const apiMock = vi.hoisted(() => ({
  getStatus: vi.fn().mockResolvedValue({
    status: 'idle',
    current_phase: '',
    state: null,
    errors: [],
  }),
  crawlOverseas: vi.fn(),
  crawlWewe: vi.fn(),
  scoreV2Task: vi.fn(),
  classifyV2Task: vi.fn(),
  autonomousApi: {
    createRun: vi.fn(),
    getRun: vi.fn(),
    cancelRun: vi.fn(),
    resumeRun: vi.fn(),
    approveApproval: vi.fn(),
    rejectApproval: vi.fn(),
    openEventSource: vi.fn(),
  },
}));

vi.mock('../api/client', () => ({
  default: apiMock,
}));

vi.mock('../hooks/useActiveTasks', () => ({
  useActiveTasks: () => ({ pipelineTask: null, draftTask: null, loading: false }),
}));

// 子组件轻量化渲染，避免不必要的依赖
vi.mock('./LiveOperationProgress', () => ({
  default: () => <div data-testid="live-operation-progress" />,
}));
vi.mock('./PipelineTaskProgress', () => ({
  default: () => <div data-testid="pipeline-task-progress" />,
}));

function makeRun(overrides: Partial<RuntimeSummary> = {}): RuntimeSummary {
  return {
    run_id: 'run-1',
    status: 'running',
    current_step: 's2',
    goal: '测试目标',
    completed_steps: ['s1'],
    failed_steps: [],
    pending_steps: ['s2'],
    evidence_count: 1,
    decision_count: 2,
    budget_usage: {
      steps: 3,
      input_tokens: 100,
      output_tokens: 200,
      tool_calls: 2,
      retries: 0,
      cost_usd: 0.001,
      consecutive_failures: 0,
    },
    pending_approvals: [],
    decision_summaries: [
      {
        step_id: 's1',
        phase: 'plan',
        action: 'plan:retrieve_articles',
        tool_name: 'retrieve_articles',
        outcome: 'planned',
        reason: '',
      },
      {
        step_id: 's2',
        phase: 'execute',
        action: 'retrieve_articles',
        tool_name: 'retrieve_articles',
        outcome: 'success',
        reason: 'ok',
      },
    ],
    evidence: [
      {
        evidence_id: 'ev-1',
        step_id: 's2',
        acceptance_index: 0,
        kind: 'tool_result',
        hash: 'h1',
        note: '证据说明',
      },
    ],
    created_at: '2026-08-10T00:00:00Z',
    updated_at: '2026-08-10T00:00:01Z',
    checkpoint_version: 2,
    ...overrides,
  };
}

describe('PipelineControl', () => {
  const defaultProps = { onComplete: vi.fn() };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders title and three-mode radio group', () => {
    render(<PipelineControl {...defaultProps} />);
    expect(screen.getByText('流水线控制')).toBeDefined();
    expect(screen.getByText('标准')).toBeDefined();
    expect(screen.getByText('AgentLoop')).toBeDefined();
    expect(screen.getByText('自主')).toBeDefined();
  });

  it('shows standard-mode trigger buttons by default', () => {
    render(<PipelineControl {...defaultProps} />);
    expect(screen.getByText('获取最新海外新闻')).toBeDefined();
    expect(screen.getByText('获取最新竞品公众号推文')).toBeDefined();
  });

  it('switches to AgentLoop mode with async task buttons', () => {
    render(<PipelineControl {...defaultProps} />);
    fireEvent.click(screen.getByText('AgentLoop'));
    expect(screen.getByText('智能评分')).toBeDefined();
    expect(screen.getByText('智能分类')).toBeDefined();
  });

  it('shows autonomous creation form without a run', () => {
    render(<PipelineControl {...defaultProps} />);
    fireEvent.click(screen.getByText('自主'));
    expect(screen.getByText('受约束全自主 Agent')).toBeDefined();
    expect(screen.getByText('目标（Goal）')).toBeDefined();
    expect(screen.getByText('验收条件（每行一条）')).toBeDefined();
    expect(screen.getByText('启动自主运行')).toBeDefined();
    expect(apiMock.autonomousApi.createRun).not.toHaveBeenCalled();
  });

  it('rejects too-short goal before calling createRun', () => {
    render(<PipelineControl {...defaultProps} />);
    fireEvent.click(screen.getByText('自主'));
    fireEvent.change(screen.getByPlaceholderText(/检索最近一周 AI 安全相关文章/), {
      target: { value: '目标' },
    });
    fireEvent.click(screen.getByText('启动自主运行'));
    expect(screen.getByText('目标至少 3 个字符')).toBeDefined();
    expect(apiMock.autonomousApi.createRun).not.toHaveBeenCalled();
  });

  it('rejects empty acceptance criteria before calling createRun', () => {
    render(<PipelineControl {...defaultProps} />);
    fireEvent.click(screen.getByText('自主'));
    fireEvent.change(screen.getByPlaceholderText(/检索最近一周 AI 安全相关文章/), {
      target: { value: '检索最近一周 AI 安全相关文章' },
    });
    fireEvent.click(screen.getByText('启动自主运行'));
    expect(screen.getByText('至少填写一条验收条件')).toBeDefined();
    expect(apiMock.autonomousApi.createRun).not.toHaveBeenCalled();
  });

  it('creates a run and renders running detail with budget/decisions/evidence', async () => {
    apiMock.autonomousApi.createRun.mockResolvedValue(makeRun());
    apiMock.autonomousApi.getRun.mockResolvedValue(makeRun());
    apiMock.autonomousApi.openEventSource.mockReturnValue({
      close: vi.fn(),
      addEventListener: vi.fn(),
    } as unknown as EventSource);

    render(<PipelineControl {...defaultProps} />);
    fireEvent.click(screen.getByText('自主'));
    fireEvent.change(screen.getByPlaceholderText(/检索最近一周 AI 安全相关文章/), {
      target: { value: '检索最近一周 AI 安全相关文章' },
    });
    fireEvent.change(screen.getByPlaceholderText(/输出文件已生成/), {
      target: { value: '输出文件已生成' },
    });
    fireEvent.click(screen.getByText('启动自主运行'));

    await waitFor(() =>
      expect(apiMock.autonomousApi.createRun).toHaveBeenCalledWith({
        goal: '检索最近一周 AI 安全相关文章',
        acceptance_criteria: ['输出文件已生成'],
        tool_chain: undefined,
      }),
    );
    expect(await screen.findByText(/run-1/)).toBeDefined();
    // 预算用量
    expect(screen.getByText(/步骤 3/)).toBeDefined();
    expect(screen.getByText(/工具调用 2/)).toBeDefined();
    // 决策摘要
    expect(screen.getByText('plan:retrieve_articles')).toBeDefined();
    // 证据
    expect(screen.getByText(/证据说明/)).toBeDefined();
    // SSE 已订阅
    expect(apiMock.autonomousApi.openEventSource).toHaveBeenCalled();
  });

  it('calls cancelRun for the active run', async () => {
    apiMock.autonomousApi.createRun.mockResolvedValue(makeRun());
    apiMock.autonomousApi.getRun.mockResolvedValue(makeRun());
    apiMock.autonomousApi.cancelRun.mockResolvedValue({
      run_id: 'run-1',
      status: 'cancel_requested',
    });
    apiMock.autonomousApi.openEventSource.mockReturnValue({
      close: vi.fn(),
      addEventListener: vi.fn(),
    } as unknown as EventSource);

    render(<PipelineControl {...defaultProps} />);
    fireEvent.click(screen.getByText('自主'));
    fireEvent.change(screen.getByPlaceholderText(/检索最近一周 AI 安全相关文章/), {
      target: { value: '检索最近一周 AI 安全相关文章' },
    });
    fireEvent.change(screen.getByPlaceholderText(/输出文件已生成/), {
      target: { value: '输出文件已生成' },
    });
    fireEvent.click(screen.getByText('启动自主运行'));
    await screen.findByText(/run-1/);

    fireEvent.click(screen.getByText('取消'));
    await waitFor(() => expect(apiMock.autonomousApi.cancelRun).toHaveBeenCalledWith('run-1'));
  });

  it('renders pending approval card and approves it', async () => {
    const waiting = makeRun({
      status: 'waiting_approval',
      pending_approvals: [
        {
          approval_id: 'ap-1',
          action: 'export_articles_csv',
          risk_level: 'L2',
          params_summary: '导出 CSV（脱敏）',
          status: 'pending',
          expires_at: null,
        },
      ],
    });
    apiMock.autonomousApi.createRun.mockResolvedValue(waiting);
    apiMock.autonomousApi.getRun.mockResolvedValue(waiting);
    apiMock.autonomousApi.approveApproval.mockResolvedValue({
      approval_id: 'ap-1',
      status: 'approved',
      run_id: 'run-1',
    });
    apiMock.autonomousApi.openEventSource.mockReturnValue({
      close: vi.fn(),
      addEventListener: vi.fn(),
    } as unknown as EventSource);

    render(<PipelineControl {...defaultProps} />);
    fireEvent.click(screen.getByText('自主'));
    fireEvent.change(screen.getByPlaceholderText(/检索最近一周 AI 安全相关文章/), {
      target: { value: '检索最近一周 AI 安全相关文章' },
    });
    fireEvent.change(screen.getByPlaceholderText(/输出文件已生成/), {
      target: { value: '输出文件已生成' },
    });
    fireEvent.click(screen.getByText('启动自主运行'));

    await screen.findByText('等待审批');
    expect(screen.getByText('恢复')).toBeDefined();
    expect(screen.getByText('export_articles_csv')).toBeDefined();
    fireEvent.click(screen.getByText('通过'));
    await waitFor(() => expect(apiMock.autonomousApi.approveApproval).toHaveBeenCalledWith('ap-1'));
  });
});
