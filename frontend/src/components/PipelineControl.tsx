/**
 * 流水线控制面板
 *
 * 提供流水线触发按钮、进度 Steps、状态显示。
 * 运行时每 2s 轮询状态，完成后自动通知父组件刷新数据。
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
  PlayCircleOutlined,
  SyncOutlined,
} from '@ant-design/icons';
import { Alert, Button, Card, Space, Steps, Tag, Typography, message } from 'antd';
import { useCallback, useEffect, useRef, useState } from 'react';
import api from '../api/client';
import type { PipelineState, PipelineStatus } from '../types';
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
  onRefresh: () => void | Promise<void>;
}

type ActionKey =
  | 'run'
  | 'crawl'
  | 'score'
  | 'overseas'
  | 'wewe'
  | 'score-v2'
  | 'classify-v2'
  | 'run-v2'
  | 'report';

interface ActiveOperation {
  key: ActionKey;
  label: string;
  message: string;
  startedAt: number;
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : '未知错误';
}

export default function PipelineControl({ onComplete, onRefresh }: PipelineControlProps) {
  const [status, setStatus] = useState<PipelineStatus>('idle');
  const [state, setState] = useState<PipelineState | null>(null);
  const [running, setRunning] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [activeTask, setActiveTask] = useState<{
    id: string;
    key: ActionKey;
    label: string;
  } | null>(null);
  const [activeOperation, setActiveOperation] = useState<ActiveOperation | null>(null);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

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

  const pollStatus = useCallback(async () => {
    try {
      const res = await api.getStatus();
      const s = res.status as PipelineStatus;
      setStatus(s);
      setErrors(res.errors || []);

      if (res.state && Object.keys(res.state).length > 0) {
        setState(res.state as PipelineState);
      }

      if (s === 'completed' || s === 'failed' || s === 'cancelled') {
        stopPolling();
        endOperation();
        if (s === 'completed') {
          message.success('流水线执行完成');
          onComplete();
        } else if (s === 'failed') {
          message.error(`流水线执行失败: ${res.errors?.join(', ') || '未知错误'}`);
        }
      }
    } catch {
      // 轮询失败不影响继续尝试
    }
  }, [endOperation, stopPolling, onComplete]);

  const startPolling = useCallback(() => {
    stopPolling();
    pollStatus(); // 立即查询一次
    pollingRef.current = setInterval(pollStatus, POLL_INTERVAL_MS);
  }, [stopPolling, pollStatus]);

  // 清理定时器
  useEffect(() => {
    return () => stopPolling();
  }, [stopPolling]);

  // ── 触发操作 ──────────────────────────────────────────────

  const trigger = useCallback(
    async (action: 'run' | 'score' | 'report', label: string, days?: number) => {
      beginOperation(action, label, `正在执行${label}，服务端完成后会自动刷新结果...`);
      message.loading({ content: `${label}中...`, key: 'pipeline', duration: 0 });

      try {
        if (action === 'run') await api.run(days || 1);
        else if (action === 'score') await api.score();
        else if (action === 'report') await api.report();
        message.success({ content: `${label}已触发`, key: 'pipeline', duration: 2 });
        startPolling();
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : '未知错误';
        console.error(`[Pipeline] ${action} failed:`, e);
        message.error({ content: `${label}失败: ${msg}`, key: 'pipeline' });
        endOperation();
        setStatus('failed');
      }
    },
    [beginOperation, endOperation, startPolling],
  );

  const handleRunFull = useCallback(() => trigger('run', '全流程', 1), [trigger]);
  const handleCrawl = useCallback(async () => {
    try {
      beginOperation('crawl', '爬取+分类', '正在创建后台任务...');
      message.loading({ content: '正在创建爬取任务...', key: 'pipeline', duration: 0 });
      const res = await api.crawl(1);
      setActiveTask({ id: res.data.task_id, key: 'crawl', label: '爬取+分类' });
      setActiveOperation(null);
      message.success({ content: '爬取任务已创建', key: 'pipeline', duration: 2 });
    } catch (error) {
      const detail = error instanceof Error ? error.message : '未知错误';
      endOperation();
      setStatus('failed');
      message.error({ content: `爬取任务创建失败: ${detail}`, key: 'pipeline' });
    }
  }, [beginOperation, endOperation]);
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
      beginOperation('score-v2', 'V2打分', '正在对候选文章进行产品相关度和事件影响度评估...');
      message.loading({ content: 'V2打分中...', key: 'scoreV2', duration: 0 });
      const res = await api.scoreV2();
      message.success({
        content: `V2打分: ${res.scored} 篇 (${res.candidates} 篇达标≥80)`,
        key: 'scoreV2',
        duration: 4,
      });
      setStatus('completed');
      await onRefresh();
    } catch {
      setStatus('failed');
      message.error({ content: 'V2打分失败', key: 'scoreV2' });
    } finally {
      endOperation();
    }
  }, [beginOperation, endOperation, onRefresh]);
  const handleReport = useCallback(() => trigger('report', '报道'), [trigger]);
  const handleClassifyV2 = useCallback(async () => {
    try {
      beginOperation('classify-v2', 'V2分类', '正在使用六分类模型逐批分析文章...');
      message.loading({ content: '6分类中...', key: 'classifyV2', duration: 0 });
      const res = await api.classifyV2();
      message.success({
        content: `6分类完成: ${res.classified} 篇 (${
          res.summary
            ? Object.entries(res.summary)
                .map(([k, v]) => `${k}:${v}`)
                .join(', ')
            : ''
        })`,
        key: 'classifyV2',
        duration: 5,
      });
      setStatus('completed');
      onComplete();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '未知错误';
      setStatus('failed');
      message.error({ content: `6分类失败: ${msg}`, key: 'classifyV2' });
    } finally {
      endOperation();
    }
  }, [beginOperation, endOperation, onComplete]);
  const handleRunV2 = useCallback(async () => {
    try {
      beginOperation('run-v2', '智能PR流水线', '正在创建后台任务...');
      message.loading({ content: '正在创建V2智能PR任务...', key: 'pipelineV2', duration: 0 });
      const res = await api.runV2(1);
      setActiveTask({ id: res.data.task_id, key: 'run-v2', label: 'V2智能PR流水线' });
      setActiveOperation(null);
      message.success({ content: 'V2任务已创建', key: 'pipelineV2', duration: 2 });
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '未知错误';
      message.error({ content: `V2流水线失败: ${msg}`, key: 'pipelineV2' });
      endOperation();
      setStatus('failed');
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

  // ── 状态标签 ──────────────────────────────────────────────

  const statusTag = () => {
    switch (status) {
      case 'idle':
        return <Tag color="default">● 空闲</Tag>;
      case 'running':
        return (
          <Tag color="processing" icon={<SyncOutlined spin />}>
            运行中
          </Tag>
        );
      case 'completed':
        return (
          <Tag color="success" icon={<CheckCircleOutlined />}>
            完成
          </Tag>
        );
      case 'failed':
        return (
          <Tag color="error" icon={<CloseCircleOutlined />}>
            失败
          </Tag>
        );
      case 'cancelled':
        return <Tag color="warning">已取消</Tag>;
    }
  };

  return (
    <Card
      title={
        <Space>
          <Text strong>流水线控制</Text>
          {statusTag()}
        </Space>
      }
      style={{ marginBottom: 16 }}
    >
      {/* 触发按钮 */}
      <Space wrap style={{ marginBottom: 16 }}>
        <Button
          type="primary"
          icon={<PlayCircleOutlined />}
          onClick={handleRunFull}
          loading={activeOperation?.key === 'run'}
          disabled={running}
        >
          全流程
        </Button>
        <Button
          icon={<CloudDownloadOutlined />}
          onClick={handleCrawl}
          disabled={running}
          loading={activeOperation?.key === 'crawl'}
        >
          爬取+分类
        </Button>
        <Button
          onClick={handleCrawlOverseas}
          disabled={running}
          loading={activeOperation?.key === 'overseas'}
        >
          海外新闻
        </Button>
        <Button
          onClick={handleCrawlWewe}
          disabled={running}
          loading={activeOperation?.key === 'wewe'}
        >
          公众号
        </Button>
        <Button
          icon={<ExperimentOutlined />}
          onClick={handleScoreV2}
          disabled={running}
          loading={activeOperation?.key === 'score-v2'}
        >
          V2打分
        </Button>
        <Button
          icon={<ExperimentOutlined />}
          onClick={handleClassifyV2}
          disabled={running}
          loading={activeOperation?.key === 'classify-v2'}
        >
          V2分类
        </Button>
        <Button
          type="primary"
          icon={<PlayCircleOutlined />}
          onClick={handleRunV2}
          loading={activeOperation?.key === 'run-v2'}
          disabled={running}
        >
          智能PR流水线
        </Button>
        <Button
          icon={<FileTextOutlined />}
          onClick={handleReport}
          disabled={running}
          loading={activeOperation?.key === 'report'}
        >
          仅报道
        </Button>
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
        <Text type="secondary">点击"全流程"开始爬取、分类、打分和报道生成</Text>
      )}

      {/* 错误展示 */}
      {errors.length > 0 && (
        <Alert
          type="error"
          message="执行错误"
          description={Array.from(new Set(errors)).map((error) => <div key={error}>{error}</div>)}
          showIcon
          closable
          style={{ marginTop: 12 }}
        />
      )}
    </Card>
  );
}
