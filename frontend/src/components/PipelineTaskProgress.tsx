import { ClockCircleOutlined, SyncOutlined } from '@ant-design/icons';
import { Alert, Card, Progress, Space, Tag, Typography } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { pipelineApi } from '../api/client';
import type { PipelineTask } from '../types';

const { Text } = Typography;
const POLL_INTERVAL_MS = 2000;

interface PipelineTaskProgressProps {
  taskId: string;
  label?: string;
  onCompleted: (task: PipelineTask) => void | Promise<void>;
  onFailed?: (task: PipelineTask) => void;
}

const PHASE_LABELS: Record<string, string> = {
  pending: '等待执行',
  crawl: '爬取内容',
  classify: '文章分类',
  classify_v2: '智能分类',
  score: '内容打分',
  score_v2: '智能打分',
  draft: '生成草稿',
  report: '生成报道',
  completed: '任务完成',
  failed: '任务失败',
  cancelled: '任务已取消',
  interrupted: '任务已中断',
};

function formatElapsed(totalSeconds: number) {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0 ? `${minutes}分${seconds.toString().padStart(2, '0')}秒` : `${seconds}秒`;
}

export default function PipelineTaskProgress({
  taskId,
  label = '后台任务',
  onCompleted,
  onFailed,
}: PipelineTaskProgressProps) {
  const [task, setTask] = useState<PipelineTask | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const onCompletedRef = useRef(onCompleted);
  const onFailedRef = useRef(onFailed);

  useEffect(() => {
    onCompletedRef.current = onCompleted;
    onFailedRef.current = onFailed;
  }, [onCompleted, onFailed]);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const nextTask = await pipelineApi.getTaskStatus(taskId);
        if (!active) return;
        setTask(nextTask);
        setPollError(null);
        if (nextTask.status === 'completed') {
          await onCompletedRef.current(nextTask);
          return;
        }
        if (['failed', 'cancelled', 'interrupted'].includes(nextTask.status)) {
          onFailedRef.current?.(nextTask);
          return;
        }
      } catch (error) {
        if (!active) return;
        setPollError(error instanceof Error ? error.message : '任务状态查询失败');
      }
      if (active) timer = setTimeout(poll, POLL_INTERVAL_MS);
    };
    void poll();
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, [taskId]);

  useEffect(() => {
    const updateElapsed = () => {
      const startedAt = task?.created_at ? new Date(task.created_at).getTime() : Date.now();
      setElapsedSeconds(Math.max(0, Math.floor((Date.now() - startedAt) / 1000)));
    };
    updateElapsed();
    const timer = setInterval(updateElapsed, 1000);
    return () => clearInterval(timer);
  }, [task?.created_at]);

  const percent = useMemo(() => {
    if (!task) return 0;
    if (task.status === 'completed') return 100;
    const total = task.progress?.total || 1;
    return Math.min(99, Math.round(((task.progress?.current || 0) / total) * 100));
  }, [task]);

  if (pollError && !task) return <Alert type="warning" showIcon message={pollError} />;
  const phase = task?.progress?.phase || 'pending';
  const current = task?.progress?.current || 0;
  const total = task?.progress?.total || 0;
  return (
    <Card size="small" style={{ width: '100%', marginBottom: 16, background: '#f7faff' }}>
      <Space direction="vertical" size={8} style={{ width: '100%' }} aria-live="polite">
        <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
          <Space>
            {!['failed', 'cancelled', 'interrupted', 'completed'].includes(
              task?.status || 'pending',
            ) && <SyncOutlined spin style={{ color: '#1677ff' }} />}
            <Text strong>{label}</Text>
            <Tag
              color={
                ['failed', 'cancelled', 'interrupted'].includes(task?.status || '')
                  ? 'error'
                  : task?.status === 'completed'
                    ? 'success'
                    : 'processing'
              }
            >
              {PHASE_LABELS[phase] || phase}
            </Tag>
          </Space>
          <Text type="secondary">
            <ClockCircleOutlined /> 已耗时 {formatElapsed(elapsedSeconds)}
          </Text>
        </Space>
        <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
          <Text>{task?.progress?.message || '任务已创建，等待执行...'}</Text>
          {total > 0 && (
            <Text type="secondary">
              步骤 {Math.min(current + 1, total)} / {total}
            </Text>
          )}
        </Space>
        <Progress
          percent={percent}
          strokeColor={{ from: '#1677ff', to: '#52c41a' }}
          status={
            ['failed', 'cancelled', 'interrupted'].includes(task?.status || '')
              ? 'exception'
              : task?.status === 'completed'
                ? 'success'
                : 'active'
          }
        />
        {task?.error && <Alert type="error" showIcon message={task.error} />}
        {pollError && <Text type="warning">状态刷新失败，将自动重试：{pollError}</Text>}
      </Space>
    </Card>
  );
}
