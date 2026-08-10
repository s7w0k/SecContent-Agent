import {
  ApiOutlined,
  ClockCircleOutlined,
  ExclamationCircleOutlined,
  ReloadOutlined,
  SyncOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Space,
  Spin,
  Tag,
  Tooltip,
  Tree,
  Typography,
} from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { pipelineApi } from '../api/client';

const { Text } = Typography;

interface MultiAgentRunTreeProps {
  runId: string;
}

interface StepRow {
  step_id: string;
  worker: string;
  status: string;
  attempt?: number;
  duration_ms?: number;
  error_message?: string;
  error_type?: string;
  input_hash?: string;
  result_hash?: string;
  reason?: string;
  order?: number;
}

interface EventRow {
  event_type: string;
  step_id: string;
  worker: string;
  attempt: number;
  duration_ms: number;
  error_type: string | null;
  status: string;
  created_at: string;
}

interface PlanDoc {
  plan_id?: string;
  planner_version?: string;
  status?: string;
  source?: string;
  rationale_summary?: string;
  steps_count?: number;
  input_snapshot_hash?: string;
  rejected_reason?: string;
  created_at?: string;
}

const STATUS_COLORS: Record<string, string> = {
  succeeded: 'green',
  completed: 'green',
  failed: 'red',
  dead_lettered: 'orange',
  skipped: 'default',
  scheduled: 'processing',
  running: 'processing',
  canceled: 'warning',
  cancelled: 'warning',
  pending: 'default',
  accepted: 'green',
  rejected: 'red',
  fallback: 'warning',
};

function statusTag(status: string) {
  const color = STATUS_COLORS[status] || 'default';
  const label = status || 'unknown';
  return (
    <Tag color={color} style={{ marginInlineEnd: 4 }}>
      {label}
    </Tag>
  );
}

function formatDuration(ms?: number) {
  if (!ms) return '—';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatTime(value?: string) {
  if (!value) return '';
  const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value);
  const date = new Date(hasTimezone ? value : `${value}Z`);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

/** 依据 ledger 步骤 order 稳定排序（无 order 时按 step_id） */
function sortSteps(steps: StepRow[]) {
  return [...steps].sort((a, b) => {
    const ao = typeof a.order === 'number' ? a.order : Number.MAX_SAFE_INTEGER;
    const bo = typeof b.order === 'number' ? b.order : Number.MAX_SAFE_INTEGER;
    if (ao !== bo) return ao - bo;
    return (a.step_id || '').localeCompare(b.step_id || '');
  });
}

/**
 * MultiAgent 编排树形视图（Step 9）。
 *
 * 只展示计划摘要、步骤、状态、耗时、重试与脱敏错误；
 * 不展示私有思维链、完整参数或业务敏感上下文。
 */
export default function MultiAgentRunTree({ runId }: MultiAgentRunTreeProps) {
  const [plan, setPlan] = useState<PlanDoc | null>(null);
  const [steps, setSteps] = useState<StepRow[]>([]);
  const [events, setEvents] = useState<EventRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!runId) return;
    setLoading(true);
    setError(null);
    try {
      const [planRes, stepsRes, eventsRes] = await Promise.all([
        pipelineApi.getRunPlan(runId),
        pipelineApi.getRunSteps(runId),
        pipelineApi.getRunEvents(runId),
      ]);
      setPlan((planRes.data.plan as PlanDoc | null) || null);
      setSteps((stepsRes.data.steps as unknown as StepRow[]) || []);
      setEvents(eventsRes.data.events || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : '编排数据加载失败');
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    void load();
  }, [load]);

  const treeData = useMemo(() => {
    const planChildren: { key: string; title: ReactNode; children?: { key: string; title: ReactNode }[] }[] = [];
    for (const step of sortSteps(steps)) {
      const attempts = step.attempt || 0;
      const retried = attempts > 1;
      planChildren.push({
        key: `step-${step.step_id}`,
        title: (
          <Space size={4} wrap>
            {statusTag(step.status)}
            <Text strong>{step.step_id}</Text>
            <Text type="secondary">[{step.worker}]</Text>
            {retried && (
              <Tooltip title={`重试次数：${attempts}`}>
                <Tag color="purple">{attempts} 次</Tag>
              </Tooltip>
            )}
            <Text type="secondary">
              <ClockCircleOutlined /> {formatDuration(step.duration_ms)}
            </Text>
            {step.error_type && <Tag color="red">{step.error_type}</Tag>}
          </Space>
        ),
        children: [
          ...(step.error_message
            ? [
                {
                  key: `step-${step.step_id}-err`,
                  title: (
                    <Text type="danger" style={{ maxWidth: 560, whiteSpace: 'pre-wrap' }}>
                      {step.error_message}
                    </Text>
                  ),
                },
              ]
            : []),
          ...(step.input_hash
            ? [
                {
                  key: `step-${step.step_id}-ih`,
                  title: (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      input_hash: {step.input_hash}
                    </Text>
                  ),
                },
              ]
            : []),
          ...(step.result_hash
            ? [
                {
                  key: `step-${step.step_id}-rh`,
                  title: (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      result_hash: {step.result_hash}
                    </Text>
                  ),
                },
              ]
            : []),
        ],
      });
    }

    const eventNodes = events.map((ev, index) => ({
      key: `ev-${index}`,
      title: (
        <Space size={4} wrap>
          {statusTag(ev.event_type)}
          {ev.step_id && <Text>{ev.step_id}</Text>}
          {ev.worker && <Text type="secondary">[{ev.worker}]</Text>}
          {ev.attempt > 0 && <Text type="secondary">attempt={ev.attempt}</Text>}
          <Text type="secondary">
            <ClockCircleOutlined /> {formatDuration(ev.duration_ms)}
          </Text>
          {ev.error_type && <Tag color="red">{ev.error_type}</Tag>}
          <Text type="secondary" style={{ fontSize: 12 }}>
            {formatTime(ev.created_at)}
          </Text>
        </Space>
      ),
    }));

    return [
      {
        key: 'plan',
        title: (
          <Space size={4} wrap>
            <ApiOutlined />
            <Text strong>计划</Text>
            {plan && statusTag(plan.status || plan.source || '')}
            {plan?.planner_version && <Text type="secondary">{plan.planner_version}</Text>}
          </Space>
        ),
        children: planChildren.length
          ? planChildren
          : [{ key: 'plan-empty', title: <Text type="secondary">暂无步骤记录</Text> }],
      },
      {
        key: 'events',
        title: (
          <Space size={4} wrap>
            <SyncOutlined />
            <Text strong>事件流</Text>
            <Text type="secondary">({events.length})</Text>
          </Space>
        ),
        children: eventNodes.length
          ? eventNodes
          : [{ key: 'ev-empty', title: <Text type="secondary">暂无事件</Text> }],
      },
    ];
  }, [plan, steps, events]);

  if (loading) {
    return (
      <Card size="small" title={`MultiAgent 编排 · ${runId}`} style={{ marginBottom: 16 }}>
        <Spin tip="加载编排数据..." style={{ width: '100%' }}>
          <div style={{ minHeight: 80 }} />
        </Spin>
      </Card>
    );
  }

  if (error) {
    return <Alert type="warning" showIcon message={`编排视图加载失败：${error}`} />;
  }

  return (
    <Card
      size="small"
      title={
        <Space size={4} wrap>
          <ApiOutlined />
          <Text strong>MultiAgent 编排</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {runId}
          </Text>
        </Space>
      }
      extra={
        <Button size="small" icon={<ReloadOutlined />} onClick={() => void load()}>
          刷新
        </Button>
      }
      style={{ marginBottom: 16 }}
    >
      {!plan && !steps.length && !events.length ? (
        <Empty description="该 run 暂无编排数据" />
      ) : (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {plan && (
            <Descriptions
              size="small"
              column={2}
              bordered
              items={[
                { key: 'status', label: '状态', children: statusTag(plan.status || plan.source || '') },
                {
                  key: 'version',
                  label: 'Planner 版本',
                  children: plan.planner_version || '—',
                },
                {
                  key: 'source',
                  label: '来源',
                  children: plan.source ? statusTag(plan.source) : '—',
                },
                {
                  key: 'steps',
                  label: '步骤数',
                  children: plan.steps_count ?? steps.length,
                },
                {
                  key: 'hash',
                  label: '输入快照',
                  children: plan.input_snapshot_hash ? (
                    <Text style={{ fontSize: 12 }}>{plan.input_snapshot_hash}</Text>
                  ) : (
                    '—'
                  ),
                  span: 2,
                },
                {
                  key: 'rationale',
                  label: '规划摘要',
                  children: plan.rationale_summary || plan.rejected_reason || '—',
                  span: 2,
                },
              ]}
            />
          )}
          <Tree
            showIcon={false}
            blockNode
            defaultExpandAll
            treeData={treeData as any}
            titleRender={(node: any) => node.title}
          />
          {events.some((ev) => ['failed', 'dead_lettered'].includes(ev.event_type)) && (
            <Alert
              type="warning"
              showIcon
              icon={<ExclamationCircleOutlined />}
              message="存在失败/死信步骤，可在步骤账本中对 dead-lettered/failed 步骤进行人工重放。"
            />
          )}
        </Space>
      )}
    </Card>
  );
}
