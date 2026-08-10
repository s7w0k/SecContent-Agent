/**
 * 流水线控制面板
 *
 * 提供三种运行模式：
 *   - 标准：同步触发海外新闻 / 公众号抓取；
 *   - AgentLoop：异步任务（V2 智能评分 / 分类），Worker 循环执行并展示进度；
 *   - 自主：受约束全自主 Agent（阶段四 4A），创建运行后展示
 *     当前步骤、预算、工具决策摘要、审批状态和证据（不展示隐藏推理）。
 *
 * Props:
 *   onComplete: () => void  — 流水线完成后的回调（刷新数据）
 */

import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  CloudDownloadOutlined,
  ExperimentOutlined,
  FileTextOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SyncOutlined,
} from '@ant-design/icons';
import { Alert, Button, Card, Input, Radio, Space, Steps, Tag, Typography, message } from 'antd';
import { useCallback, useEffect, useRef, useState } from 'react';
import api from '../api/client';
import { useActiveTasks } from '../hooks/useActiveTasks';
import type {
  PipelineState,
  PipelineStatus,
  RuntimeEventEnvelope,
  RuntimeStatus,
  RuntimeSummary,
} from '../types';
import LiveOperationProgress from './LiveOperationProgress';
import PipelineTaskProgress from './PipelineTaskProgress';

const { Text } = Typography;
const POLL_INTERVAL_MS = 2000;

const PHASE_STEPS = [
  { title: '爬取', icon: <CloudDownloadOutlined />, key: 'crawled_count' as const },
  { title: '分类', icon: <ExperimentOutlined />, key: 'classified_count' as const },
  { title: '打分', icon: <SyncOutlined />, key: 'scored_count' as const },
  { title: '报道', icon: <FileTextOutlined />, key: 'report_count' as const },
];

interface PipelineControlProps {
  onComplete: () => void;
}

type PipelineMode = 'standard' | 'agentloop' | 'autonomous';

type ActionKey = 'overseas' | 'wewe' | 'score-v2' | 'classify-v2';

interface ActiveOperation {
  key: ActionKey;
  label: string;
  message: string;
  startedAt: number;
}

const TERMINAL_RUNTIME_STATUSES: ReadonlySet<string> = new Set([
  'completed',
  'failed',
  'canceled',
  'budget_exceeded',
  'stopped',
]);

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : '未知错误';
}

function runtimeStatusTag(status: RuntimeStatus) {
  switch (status) {
    case 'completed':
      return (
        <Tag color="success" icon={<CheckCircleOutlined />}>
          已完成
        </Tag>
      );
    case 'failed':
      return (
        <Tag color="error" icon={<CloseCircleOutlined />}>
          失败
        </Tag>
      );
    case 'canceled':
      return <Tag color="warning">已取消</Tag>;
    case 'waiting_approval':
      return (
        <Tag color="gold" icon={<SafetyCertificateOutlined />}>
          等待审批
        </Tag>
      );
    case 'budget_exceeded':
      return <Tag color="error">预算超限</Tag>;
    case 'stopped':
      return <Tag color="default">已停止</Tag>;
    case 'cancel_requested':
      return <Tag color="warning">取消中</Tag>;
    default:
      return (
        <Tag color="processing" icon={<SyncOutlined spin />}>
          运行中
        </Tag>
      );
  }
}

export default function PipelineControl({ onComplete }: PipelineControlProps) {
  const [mode, setMode] = useState<PipelineMode>('standard');
  const [status, setStatus] = useState<PipelineStatus>('idle');
  const [state] = useState<PipelineState | null>(null);
  const [running, setRunning] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [activeTask, setActiveTask] = useState<{
    id: string;
    key: ActionKey;
    label: string;
  } | null>(null);
  const [activeOperation, setActiveOperation] = useState<ActiveOperation | null>(null);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── 自主模式状态 ─────────────────────────────────────────
  const [autonomousActive, setAutonomousActive] = useState(false);
  const [run, setRun] = useState<RuntimeSummary | null>(null);
  const [runtimeEvents, setRuntimeEvents] = useState<RuntimeEventEnvelope[]>([]);
  const [goalInput, setGoalInput] = useState('');
  const [criteriaInput, setCriteriaInput] = useState('');
  const [chainInput, setChainInput] = useState('');
  const [autonomousError, setAutonomousError] = useState('');
  const runPollingRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const eventSourceRef = useRef<EventSource | null>(null);

  // ── 页面重新挂载时恢复进行中的任务 ────────────────────────
  const { pipelineTask: restoredTask } = useActiveTasks();
  const restoredRef = useRef(false);
  useEffect(() => {
    if (restoredTask && !activeTask && !restoredRef.current) {
      restoredRef.current = true;
      setActiveTask({
        id: restoredTask.id,
        key: restoredTask.key as ActionKey,
        label: restoredTask.label,
      });
      setRunning(true);
      setStatus('running');
    }
  }, [restoredTask, activeTask]);

  const beginOperation = useCallback((key: ActionKey, label: string, operationMessage: string) => {
    setRunning(true);
    setStatus('running');
    setErrors([]);
    setActiveOperation({ key, label, message: operationMessage, startedAt: Date.now() });
  }, []);

  const endOperation = useCallback(() => {
    setRunning(false);
    setActiveOperation(null);
  }, []);

  const stopPolling = useCallback(() => {
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
  }, []);

  // 清理定时器
  useEffect(() => {
    return () => stopPolling();
  }, [stopPolling]);

  // ── 自主模式：轮询运行详情 + SSE 事件流 ──────────────────
  const stopRunStream = useCallback(() => {
    if (runPollingRef.current) {
      clearInterval(runPollingRef.current);
      runPollingRef.current = null;
    }
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
  }, []);

  const refreshRun = useCallback(
    async (runId: string) => {
      try {
        const detail = await api.autonomousApi.getRun(runId);
        setRun(detail);
        if (TERMINAL_RUNTIME_STATUSES.has(detail.status)) {
          stopRunStream();
          setAutonomousActive(false);
        }
      } catch (err) {
        setAutonomousError(`读取运行详情失败: ${errorMessage(err)}`);
        stopRunStream();
        setAutonomousActive(false);
      }
    },
    [stopRunStream],
  );

  useEffect(() => {
    if (!run) return;
    refreshRun(run.run_id);
    runPollingRef.current = setInterval(() => {
      void refreshRun(run.run_id);
    }, POLL_INTERVAL_MS);
    // SSE 事件流（Last-Event-ID 断线续传由 EventSource 自动携带）
    eventSourceRef.current = api.autonomousApi.openEventSource(run.run_id, (event) => {
      setRuntimeEvents((prev) => [...prev.slice(-49), event]);
    });
    return () => stopRunStream();
  }, [run, refreshRun, stopRunStream]);

  useEffect(() => {
    return () => stopRunStream();
  }, [stopRunStream]);

  const handleStartAutonomous = useCallback(async () => {
    const goal = goalInput.trim();
    const criteria = criteriaInput
      .split(/[\n,，]+/)
      .map((c) => c.trim())
      .filter(Boolean);
    if (goal.length < 3) {
      setAutonomousError('目标至少 3 个字符');
      return;
    }
    if (criteria.length === 0) {
      setAutonomousError('至少填写一条验收条件');
      return;
    }
    setAutonomousError('');
    try {
      const toolChain = chainInput
        .split(/[\n,，]+/)
        .map((t) => t.trim())
        .filter(Boolean);
      const created = await api.autonomousApi.createRun({
        goal,
        acceptance_criteria: criteria,
        tool_chain: toolChain.length > 0 ? toolChain : undefined,
      });
      setRun(created);
      setRuntimeEvents([]);
      setAutonomousActive(true);
      message.success('自主运行已启动');
    } catch (err) {
      setAutonomousError(`创建运行失败: ${errorMessage(err)}`);
    }
  }, [goalInput, criteriaInput, chainInput]);

  const handleCancelAutonomous = useCallback(async () => {
    if (!run) return;
    try {
      await api.autonomousApi.cancelRun(run.run_id);
      await refreshRun(run.run_id);
      message.info('已请求取消，运行将在安全点停止');
    } catch (err) {
      setAutonomousError(`取消失败: ${errorMessage(err)}`);
    }
  }, [run, refreshRun]);

  const handleResumeAutonomous = useCallback(async () => {
    if (!run) return;
    try {
      await api.autonomousApi.resumeRun(run.run_id);
      setAutonomousActive(true);
      await refreshRun(run.run_id);
      message.success('运行已恢复');
    } catch (err) {
      setAutonomousError(`恢复失败: ${errorMessage(err)}`);
    }
  }, [run, refreshRun]);

  const handleApproval = useCallback(
    async (approvalId: string, decision: 'approve' | 'reject') => {
      if (!run) return;
      try {
        if (decision === 'approve') {
          await api.autonomousApi.approveApproval(approvalId);
          message.success('已通过审批');
        } else {
          await api.autonomousApi.rejectApproval(approvalId);
          message.info('已拒绝审批');
        }
        await refreshRun(run.run_id);
      } catch (err) {
        setAutonomousError(`审批操作失败: ${errorMessage(err)}`);
      }
    },
    [run, refreshRun],
  );

  // ── 触发操作 ──────────────────────────────────────────────

  const handleCrawlOverseas = useCallback(async () => {
    beginOperation('overseas', '海外新闻', '正在连接海外新闻服务并抓取、解析、保存文章...');
    message.loading({ content: '海外新闻爬取中，预计 1-2 分钟...', key: 'overseas', duration: 0 });
    try {
      const res = await api.crawlOverseas(1);
      const siteDetail = res.per_site
        ? Object.entries(res.per_site)
            .filter(([, count]) => count > 0)
            .map(([name, count]) => `${name}: ${count}`)
            .join('  ')
        : '';
      message.success({
        content: `海外新闻: ${res.saved} 篇入库 (共 ${res.total || 0} 篇)${siteDetail ? `  |  ${siteDetail}` : ''}`,
        key: 'overseas',
        duration: 6,
      });
      setStatus('completed');
      onComplete();
    } catch (error: unknown) {
      setStatus('failed');
      message.error({ content: `海外爬取失败: ${errorMessage(error)}`, key: 'overseas' });
    } finally {
      endOperation();
    }
  }, [beginOperation, endOperation, onComplete]);
  const handleCrawlWewe = useCallback(async () => {
    beginOperation('wewe', '公众号', '正在读取公众号 RSS、解析文章并保存...');
    message.loading({ content: '公众号爬取中...', key: 'wewe', duration: 0 });
    try {
      const res = await api.crawlWewe();
      message.success({ content: `公众号: ${res.saved} 篇入库`, key: 'wewe', duration: 4 });
      setStatus('completed');
      onComplete();
    } catch (error: unknown) {
      setStatus('failed');
      message.error({ content: `公众号爬取失败: ${errorMessage(error)}`, key: 'wewe' });
    } finally {
      endOperation();
    }
  }, [beginOperation, endOperation, onComplete]);
  const handleScoreV2 = useCallback(async () => {
    try {
      beginOperation('score-v2', 'V2打分', '正在统计待打分文章并创建后台任务...');
      message.loading({ content: '正在创建V2打分任务...', key: 'scoreV2', duration: 0 });
      const res = await api.scoreV2Task();
      setActiveTask({ id: res.data.task_id, key: 'score-v2', label: 'V2打分' });
      setActiveOperation(null);
      message.success({
        content: `V2打分任务已创建，共 ${res.data.total || 0} 篇`,
        key: 'scoreV2',
        duration: 3,
      });
    } catch (error: unknown) {
      setStatus('failed');
      endOperation();
      message.error({ content: `V2打分任务创建失败: ${errorMessage(error)}`, key: 'scoreV2' });
    }
  }, [beginOperation, endOperation]);
  const handleClassifyV2 = useCallback(async () => {
    try {
      beginOperation('classify-v2', 'V2分类', '正在统计待分类文章并创建后台任务...');
      message.loading({ content: '正在创建V2分类任务...', key: 'classifyV2', duration: 0 });
      const res = await api.classifyV2Task();
      setActiveTask({ id: res.data.task_id, key: 'classify-v2', label: 'V2分类' });
      setActiveOperation(null);
      message.success({
        content: `V2分类任务已创建，共 ${res.data.total || 0} 篇`,
        key: 'classifyV2',
        duration: 3,
      });
    } catch (error: unknown) {
      setStatus('failed');
      endOperation();
      message.error({
        content: `V2分类任务创建失败: ${errorMessage(error)}`,
        key: 'classifyV2',
      });
    }
  }, [beginOperation, endOperation]);

  // ── 当前阶段索引 ──────────────────────────────────────────

  const phaseIndex = state
    ? PHASE_STEPS.findIndex((s) => {
        if (state.report_count > 0) return s.key === 'report_count';
        if (state.scored_count > 0) return s.key === 'scored_count';
        if (state.classified_count > 0) return s.key === 'classified_count';
        if (state.crawled_count > 0) return s.key === 'crawled_count';
        return false;
      })
    : -1;

  const renderAutonomousPanel = () => (
    <div data-testid="autonomous-panel">
      {!run && (
        <>
          <Alert
            type="info"
            showIcon
            message="受约束全自主 Agent"
            description="自主模式会按目标与验收条件自动规划、执行工具并校验结果；超出策略的动作需要人工审批。运行详情仅展示脱敏的决策摘要与证据，不展示内部推理。"
            style={{ marginBottom: 16 }}
          />
          <Space direction="vertical" style={{ width: '100%' }} size="middle">
            <div>
              <Text strong>目标（Goal）</Text>
              <Input.TextArea
                rows={2}
                placeholder="例如：检索最近一周 AI 安全相关文章并输出评分报告"
                value={goalInput}
                onChange={(e) => setGoalInput(e.target.value)}
                maxLength={2000}
              />
            </div>
            <div>
              <Text strong>验收条件（每行一条）</Text>
              <Input.TextArea
                rows={3}
                placeholder={'例如：\n输出文件已生成\n覆盖至少 3 个来源'}
                value={criteriaInput}
                onChange={(e) => setCriteriaInput(e.target.value)}
              />
            </div>
            <div>
              <Text strong>工具链（可选，逗号分隔，留空使用默认）</Text>
              <Input
                placeholder="retrieve_articles, classify_articles, score_articles, export_articles_csv"
                value={chainInput}
                onChange={(e) => setChainInput(e.target.value)}
              />
            </div>
            <Button
              type="primary"
              icon={<RobotOutlined />}
              onClick={handleStartAutonomous}
              loading={autonomousActive}
            >
              启动自主运行
            </Button>
          </Space>
        </>
      )}

      {run && (
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
          <div>
            <Space wrap>
              <Text strong>运行 {run.run_id}</Text>
              {runtimeStatusTag(run.status)}
              {!TERMINAL_RUNTIME_STATUSES.has(run.status) && (
                <Button
                  size="small"
                  icon={<PauseCircleOutlined />}
                  onClick={handleCancelAutonomous}
                >
                  取消
                </Button>
              )}
              {run.status === 'waiting_approval' && (
                <Button size="small" icon={<PlayCircleOutlined />} onClick={handleResumeAutonomous}>
                  恢复
                </Button>
              )}
            </Space>
          </div>
          <div>
            <Text type="secondary">目标：</Text>
            <Text>{run.goal}</Text>
          </div>
          <div>
            <Text type="secondary">当前步骤：</Text>
            <Text code>{run.current_step || '—'}</Text>
            <Text type="secondary" style={{ marginLeft: 16 }}>
              已完成 {run.completed_steps.length} 步 / 失败 {run.failed_steps.length} 步
            </Text>
          </div>
          <div>
            <Text strong>预算用量</Text>
            <div>
              <Tag>步骤 {run.budget_usage.steps}</Tag>
              <Tag>工具调用 {run.budget_usage.tool_calls}</Tag>
              <Tag>输入 tokens {run.budget_usage.input_tokens}</Tag>
              <Tag>输出 tokens {run.budget_usage.output_tokens}</Tag>
              <Tag>重试 {run.budget_usage.retries}</Tag>
              <Tag>成本 ${run.budget_usage.cost_usd.toFixed(4)}</Tag>
            </div>
          </div>

          {run.pending_approvals.length > 0 && (
            <div>
              <Text strong>待审批</Text>
              {run.pending_approvals.map((approval) => (
                <Card size="small" key={approval.approval_id} style={{ marginTop: 8 }}>
                  <Space direction="vertical" style={{ width: '100%' }}>
                    <Space wrap>
                      <Tag color="gold">{approval.action}</Tag>
                      <Tag>{approval.risk_level}</Tag>
                      <Tag color={approval.status === 'pending' ? 'processing' : 'default'}>
                        {approval.status}
                      </Tag>
                    </Space>
                    <Text type="secondary">{approval.params_summary}</Text>
                    {approval.status === 'pending' && (
                      <Space>
                        <Button
                          size="small"
                          type="primary"
                          icon={<SafetyCertificateOutlined />}
                          onClick={() => handleApproval(approval.approval_id, 'approve')}
                        >
                          通过
                        </Button>
                        <Button
                          size="small"
                          danger
                          onClick={() => handleApproval(approval.approval_id, 'reject')}
                        >
                          拒绝
                        </Button>
                      </Space>
                    )}
                  </Space>
                </Card>
              ))}
            </div>
          )}

          <div>
            <Text strong>
              决策摘要（{run.decision_count} 条，最近 {run.decision_summaries.length} 条）
            </Text>
            {run.decision_summaries.length === 0 ? (
              <div>
                <Text type="secondary">暂无</Text>
              </div>
            ) : (
              run.decision_summaries.slice(-6).map((d, i) => (
                <div key={`${d.step_id}-${i}`} style={{ marginTop: 4 }}>
                  <Tag>{d.phase}</Tag>
                  <Text code>{d.action}</Text>
                  <Tag
                    color={
                      d.outcome === 'success'
                        ? 'success'
                        : d.outcome === 'failed'
                          ? 'error'
                          : 'default'
                    }
                  >
                    {d.outcome}
                  </Tag>
                  {d.reason && <Text type="secondary"> {d.reason}</Text>}
                </div>
              ))
            )}
          </div>

          <div>
            <Text strong>证据</Text>
            <Tag style={{ marginLeft: 8 }}>{run.evidence_count} 条</Tag>
            {run.evidence.slice(-3).map((e) => (
              <div key={e.evidence_id} style={{ marginTop: 4 }}>
                <Tag>{e.kind}</Tag>
                <Text type="secondary">
                  {e.acceptance_index !== null ? `验收#${e.acceptance_index}` : ''} {e.note}
                </Text>
              </div>
            ))}
          </div>

          {runtimeEvents.length > 0 && (
            <div>
              <Text strong>事件流（最近 {runtimeEvents.length} 条）</Text>
              <div style={{ maxHeight: 160, overflow: 'auto', marginTop: 4 }}>
                {runtimeEvents.slice(-8).map((ev) => (
                  <div key={ev.sequence} style={{ marginTop: 2 }}>
                    <Text type="secondary">#{ev.sequence}</Text> <Tag>{ev.event_type}</Tag>
                    {typeof ev.payload?.tool_name === 'string' && (
                      <Text code>{ev.payload.tool_name}</Text>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </Space>
      )}
    </div>
  );

  return (
    <Card
      title={
        <Space>
          <Text strong>流水线控制</Text>
          <Radio.Group
            value={mode}
            onChange={(e) => setMode(e.target.value as PipelineMode)}
            size="small"
            optionType="button"
            buttonStyle="solid"
          >
            <Radio.Button value="standard">标准</Radio.Button>
            <Radio.Button value="agentloop">AgentLoop</Radio.Button>
            <Radio.Button value="autonomous">自主</Radio.Button>
          </Radio.Group>
        </Space>
      }
      style={{ marginBottom: 16 }}
    >
      {mode === 'autonomous' ? (
        renderAutonomousPanel()
      ) : (
        <>
          {/* 触发按钮 */}
          <Space wrap style={{ marginBottom: 16 }}>
            {mode === 'standard' ? (
              <>
                <Button
                  icon={<CloudDownloadOutlined />}
                  onClick={handleCrawlOverseas}
                  disabled={running}
                  loading={activeOperation?.key === 'overseas'}
                >
                  获取最新海外新闻
                </Button>
                <Button
                  onClick={handleCrawlWewe}
                  disabled={running}
                  loading={activeOperation?.key === 'wewe'}
                >
                  获取最新竞品公众号推文
                </Button>
              </>
            ) : (
              <>
                <Button
                  icon={<ExperimentOutlined />}
                  onClick={handleScoreV2}
                  disabled={running}
                  loading={activeOperation?.key === 'score-v2'}
                >
                  智能评分
                </Button>
                <Button
                  icon={<ExperimentOutlined />}
                  onClick={handleClassifyV2}
                  disabled={running}
                  loading={activeOperation?.key === 'classify-v2'}
                >
                  智能分类
                </Button>
              </>
            )}
          </Space>

          {activeOperation && (
            <LiveOperationProgress
              label={activeOperation.label}
              message={activeOperation.message}
              startedAt={activeOperation.startedAt}
            />
          )}

          {activeTask && (
            <PipelineTaskProgress
              taskId={activeTask.id}
              label={activeTask.label}
              onCompleted={() => {
                const label = activeTask.label;
                setActiveTask(null);
                endOperation();
                setStatus('completed');
                message.success(`${label}执行完成`);
                onComplete();
              }}
              onFailed={(task) => {
                const label = activeTask.label;
                setActiveTask(null);
                endOperation();
                setStatus('failed');
                setErrors([task.error || '未知错误']);
                message.error(`${label}失败: ${task.error || '未知错误'}`);
              }}
            />
          )}

          {/* 进度 Steps */}
          {state && (
            <Steps
              current={phaseIndex >= 0 ? phaseIndex : 0}
              status={status === 'failed' ? 'error' : status === 'completed' ? 'finish' : 'process'}
              size="small"
              items={PHASE_STEPS.map((s) => ({
                title: s.title,
                description: `${state[s.key]} 篇`,
              }))}
            />
          )}

          {/* 首次使用提示 */}
          {!state && status === 'idle' && (
            <Text type="secondary">
              {mode === 'standard'
                ? '标准模式：点击上方按钮获取最新文章'
                : 'AgentLoop 模式：创建后台任务，由 Worker 循环执行并实时展示进度'}
            </Text>
          )}

          {/* 错误展示 */}
          {errors.length > 0 && (
            <Alert
              type="error"
              message="执行错误"
              description={Array.from(new Set(errors)).map((error) => (
                <div key={error}>{error}</div>
              ))}
              showIcon
              closable
              style={{ marginTop: 12 }}
            />
          )}
        </>
      )}

      {/* 自主模式错误展示 */}
      {mode === 'autonomous' && autonomousError && (
        <Alert
          type="error"
          message="自主运行错误"
          description={autonomousError}
          showIcon
          closable
          onClose={() => setAutonomousError('')}
          style={{ marginTop: 12 }}
        />
      )}
    </Card>
  );
}
