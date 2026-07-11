import { Alert, Progress, Space, Tag, Typography } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { pipelineApi } from '../api/client';
import type { PipelineTask } from '../types';

const { Text } = Typography;
const POLL_INTERVAL_MS = 2000;

interface PipelineTaskProgressProps {
  taskId: string;
  onCompleted: (task: PipelineTask) => void | Promise<void>;
  onFailed?: (task: PipelineTask) => void;
}

export default function PipelineTaskProgress({
  taskId,
  onCompleted,
  onFailed,
}: PipelineTaskProgressProps) {
  const [task, setTask] = useState<PipelineTask | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);

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
          await onCompleted(nextTask);
          return;
        }
        if (nextTask.status === 'failed') {
          onFailed?.(nextTask);
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
  }, [onCompleted, onFailed, taskId]);

  const percent = useMemo(() => {
    if (!task) return 0;
    if (task.status === 'completed') return 100;
    const total = task.progress?.total || 1;
    return Math.min(99, Math.round(((task.progress?.current || 0) / total) * 100));
  }, [task]);

  if (pollError && !task) return <Alert type="warning" showIcon message={pollError} />;
  return (
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      <Space>
        <Tag
          color={
            task?.status === 'failed'
              ? 'error'
              : task?.status === 'completed'
                ? 'success'
                : 'processing'
          }
        >
          {task?.status || 'pending'}
        </Tag>
        <Text>{task?.progress?.message || '任务已创建，等待执行...'}</Text>
      </Space>
      <Progress
        percent={percent}
        status={
          task?.status === 'failed'
            ? 'exception'
            : task?.status === 'completed'
              ? 'success'
              : 'active'
        }
      />
      {task?.error && <Alert type="error" showIcon message={task.error} />}
      {pollError && <Text type="warning">状态刷新失败，将自动重试：{pollError}</Text>}
    </Space>
  );
}
