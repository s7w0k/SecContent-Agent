import {
  CopyOutlined,
  PauseCircleOutlined,
  PlusOutlined,
  RobotOutlined,
  SendOutlined,
  StopOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Button,
  Descriptions,
  Empty,
  Input,
  Space,
  Spin,
  Tag,
  Typography,
  message,
} from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { agentApi } from '../api/client';
import AgentCandidateCards from '../components/AgentCandidateCards';
import type { AgentCandidate, AgentRun } from '../types';
import styles from './AgentWorkspace.module.css';

const { Text, Title } = Typography;
const { TextArea } = Input;

const TERMINAL = new Set(['completed', 'failed', 'canceled']);

const INTENT_NAMES: Record<string, string> = {
  generate_draft: '生成初稿',
  search_and_rank: '搜索并挑选新闻',
  search_and_draft: '搜索新闻并写稿',
  curate_news: '精选新闻',
  revise: '修订稿件',
  save: '保存主稿',
  ask_status: '查询任务状态',
  cancel: '取消任务',
  unknown: '未识别意图',
};

function statusTag(status: AgentRun['status']) {
  const colors: Record<string, string> = {
    completed: 'success',
    failed: 'error',
    canceled: 'default',
    waiting_user: 'gold',
    waiting_approval: 'orange',
    running: 'processing',
    pending: 'default',
  };
  const labels: Record<string, string> = {
    pending: '等待执行',
    running: '执行中',
    waiting_user: '等待选择',
    waiting_approval: '等待审批',
    completed: '已完成',
    failed: '失败',
    canceled: '已取消',
  };
  return <Tag color={colors[status]}>{labels[status] || status}</Tag>;
}

function requestMessage(error: unknown) {
  if (typeof error === 'object' && error && 'response' in error) {
    const detail = (error as { response?: { data?: { detail?: string } } }).response?.data?.detail;
    if (detail) return detail;
  }
  return error instanceof Error ? error.message : '请求失败';
}

/** 从 AgentRun.result 中提取候选列表（search 类 intent 返回 items） */
function resultCandidates(result: Record<string, unknown>): AgentCandidate[] {
  const items = result.items;
  return Array.isArray(items) ? (items as unknown as AgentCandidate[]) : [];
}

interface AgentWorkspaceProps {
  onLegacyFallback?: () => void;
}

export default function AgentWorkspace({ onLegacyFallback }: AgentWorkspaceProps) {
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [run, setRun] = useState<AgentRun | null>(null);
  const [input, setInput] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const sourceRef = useRef<EventSource | null>(null);

  const refreshHistory = useCallback(async () => {
    try {
      setRuns(await agentApi.listRuns(30));
    } catch (error) {
      const status = typeof error === 'object' && error && 'response' in error
        ? (error as { response?: { status?: number } }).response?.status
        : undefined;
      if ((status === 404 || status === 503) && onLegacyFallback) {
        onLegacyFallback();
      } else {
        message.error(requestMessage(error));
      }
    } finally {
      setLoadingHistory(false);
    }
  }, [onLegacyFallback]);

  const refreshRun = useCallback(async (runId: string) => {
    const current = await agentApi.getRun(runId);
    setRun(current);
    return current;
  }, []);

  useEffect(() => {
    refreshHistory();
  }, [refreshHistory]);

  useEffect(() => {
    sourceRef.current?.close();
    if (!run || TERMINAL.has(run.status) || run.status === 'waiting_user') return;
    const source = agentApi.openEventSource(run.run_id, () => {
      refreshRun(run.run_id).catch(() => undefined);
    });
    sourceRef.current = source;
    const timer = window.setInterval(() => {
      refreshRun(run.run_id).then((next) => {
        if (TERMINAL.has(next.status) || next.status.startsWith('waiting_')) {
          window.clearInterval(timer);
          source.close();
          refreshHistory();
        }
      }).catch(() => undefined);
    }, 1500);
    return () => {
      window.clearInterval(timer);
      source.close();
    };
  }, [run?.run_id, run?.status, refreshHistory, refreshRun]);

  /** 提交一轮对话（新任务或继续回答） */
  const submitTurn = async (content: string) => {
    const text = content.trim();
    if (!text || !run) return;
    setSubmitting(true);
    try {
      const result = await agentApi.submitTurn({
        content: text,
        // 继续同一任务时复用 thread_id，保持跨轮状态
        thread_id: run.thread_id || undefined,
        task_id: run.task_id || undefined,
      });
      setRun(result.run);
      setInput('');
      await refreshHistory();
    } catch (error) {
      message.error(requestMessage(error));
    } finally {
      setSubmitting(false);
    }
  };

  const startRun = async () => {
    const goal = input.trim();
    if (!goal) return;
    setSubmitting(true);
    try {
      const result = await agentApi.submitTurn({ content: goal });
      setRun(result.run);
      setInput('');
      await refreshHistory();
    } catch (error) {
      message.error(requestMessage(error));
    } finally {
      setSubmitting(false);
    }
  };

  const approve = async () => {
    if (!run) return;
    setSubmitting(true);
    try {
      const updated = await agentApi.approveRun(run.run_id);
      setRun(updated);
      await refreshHistory();
    } catch (error) {
      message.error(requestMessage(error));
    } finally {
      setSubmitting(false);
    }
  };

  const candidates = useMemo(
    () => (run ? resultCandidates(run.result) : []),
    [run],
  );
  const questions = run?.questions || [];
  const intentName = run ? INTENT_NAMES[run.intent] || run.intent : '';

  return (
    <div className={styles.workspace}>
      <aside className={styles.history}>
        <div className={styles.historyHeader}>
          <Text strong>任务</Text>
          <Button type="text" icon={<PlusOutlined />} aria-label="新建任务" onClick={() => setRun(null)} />
        </div>
        <div className={styles.historyList}>
          {loadingHistory ? <Spin size="small" /> : runs.map((item) => (
            <button
              type="button"
              key={item.run_id}
              className={`${styles.historyButton} ${run?.run_id === item.run_id ? styles.historyButtonActive : ''}`}
              onClick={() => setRun(item)}
            >
              <Text ellipsis style={{ display: 'block' }}>{INTENT_NAMES[item.intent] || item.intent || item.run_id}</Text>
              <Space size={4} style={{ marginTop: 5 }}>{statusTag(item.status)}</Space>
            </button>
          ))}
        </div>
      </aside>

      <main className={styles.main}>
        <header className={styles.runHeader}>
          <Space>
            <RobotOutlined style={{ color: '#167a72', fontSize: 20 }} />
            <Text strong>{run ? intentName || run.run_id : 'Agent 工作台'}</Text>
            {run && statusTag(run.status)}
          </Space>
          {run && !TERMINAL.has(run.status) && (
            <Button danger type="text" icon={<StopOutlined />} onClick={async () => {
              await agentApi.cancelRun(run.run_id);
              await refreshRun(run.run_id);
            }}>取消</Button>
          )}
        </header>

        <div className={styles.content}>
          <div className={styles.contentInner}>
            {!run && <div className={styles.empty}><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="新任务" /></div>}

            {run && (
              <>
                <section className={styles.progressBand} aria-label="任务状态">
                  <Title level={5}>任务理解</Title>
                  <Descriptions size="small" column={1}>
                    <Descriptions.Item label="意图">{intentName || '-'}</Descriptions.Item>
                    {run.changed_slots.length > 0 && (
                      <Descriptions.Item label="变更槽位">{run.changed_slots.join('、')}</Descriptions.Item>
                    )}
                    {run.invalidated_steps.length > 0 && (
                      <Descriptions.Item label="失效步骤">{run.invalidated_steps.join('、')}</Descriptions.Item>
                    )}
                    {run.assumptions.length > 0 && (
                      <Descriptions.Item label="假设">
                        {run.assumptions.map((item) => <div key={item}>{item}</div>)}
                      </Descriptions.Item>
                    )}
                  </Descriptions>
                </section>

                {run.status === 'waiting_user' && (
                  <section className={styles.questionBand}>
                    <Alert
                      type="warning"
                      showIcon
                      icon={<PauseCircleOutlined />}
                      message={questions[0]?.question || '需要你的选择'}
                    />
                    {questions.map((question, index) => (
                      <div key={`${question.slot}-${index}`} style={{ marginTop: 6 }}>
                        <Text type="secondary">{question.slot}</Text>
                        {question.reason && <div><Text type="secondary" style={{ fontSize: 12 }}>{question.reason}</Text></div>}
                      </div>
                    ))}
                    {candidates.length > 0 && (
                      <AgentCandidateCards
                        candidates={candidates}
                        onSelect={(candidate) => submitTurn(candidate.title)}
                      />
                    )}
                    {candidates.length === 0 && (
                      <Button
                        onClick={() => submitTurn(input || '由你决定')}
                        loading={submitting}
                      >由你决定</Button>
                    )}
                  </section>
                )}

                {run.status === 'waiting_approval' && (
                  <section className={styles.approvalBand}>
                    <Alert
                      type="info"
                      showIcon
                      icon={<PauseCircleOutlined />}
                      message="任务需要审批后才能继续"
                    />
                    <Space style={{ marginTop: 14 }}>
                      <Button type="primary" onClick={approve} loading={submitting}>批准继续</Button>
                      <Button danger onClick={() => agentApi.cancelRun(run.run_id).then(() => refreshRun(run.run_id))}>拒绝并停止</Button>
                    </Space>
                  </section>
                )}

                {run.status === 'completed' && (
                  <section className={styles.resultBand}>
                    <div className={styles.resultHeader}>
                      <Title level={5} style={{ margin: 0 }}>结果</Title>
                      <Button icon={<CopyOutlined />} onClick={() => {
                        navigator.clipboard.writeText(JSON.stringify(run.result, null, 2));
                        message.success('结果已复制');
                      }}>复制结果</Button>
                    </div>
                    {typeof run.result.message === 'string' && (
                      <Text>{String(run.result.message)}</Text>
                    )}
                    {candidates.length > 0 && (
                      <>
                        <div style={{ marginTop: 12 }}><Text strong>检索到的候选</Text></div>
                        <AgentCandidateCards
                          candidates={candidates}
                          selectedId={
                            (run.result.selection as { selected?: { article_id?: string } } | undefined)
                              ?.selected?.article_id
                          }
                          onSelect={(candidate) => submitTurn(candidate.title)}
                        />
                      </>
                    )}
                    {run.result.task_id !== undefined && (
                      <Descriptions size="small" column={1} style={{ marginTop: 12 }}>
                        <Descriptions.Item label="任务 ID">{String(run.result.task_id)}</Descriptions.Item>
                      </Descriptions>
                    )}
                  </section>
                )}

                {run.status === 'failed' && (
                  <Alert type="error" showIcon message="任务未完成" description={run.error || run.status} />
                )}
              </>
            )}
          </div>
        </div>

        <div className={styles.composer}>
          <div className={styles.composerInner}>
            <TextArea
              value={input}
              onChange={(event) => setInput(event.target.value)}
              autoSize={{ minRows: 2, maxRows: 5 }}
              placeholder={run?.status === 'waiting_user' ? '选择候选或继续描述需求' : '描述任务目标，例如：搜索智能体安全相关的新闻并写一篇 PR'}
              onPressEnter={(event) => {
                if (!event.shiftKey) {
                  event.preventDefault();
                  if (run?.status === 'waiting_user') {
                    submitTurn(input);
                  } else {
                    startRun();
                  }
                }
              }}
            />
            <div className={styles.composerActions} style={{ marginTop: 8 }}>
              <Text type="secondary">{run?.run_id || ''}</Text>
              <Button
                type="primary"
                icon={<SendOutlined />}
                loading={submitting}
                disabled={!input.trim()}
                onClick={() => (run?.status === 'waiting_user' ? submitTurn(input) : startRun())}
              >
                {run?.status === 'waiting_user' ? '回答' : '发送'}
              </Button>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
