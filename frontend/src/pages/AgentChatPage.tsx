/**
 * 聊天式 Agent 工作台 —— 市面主流 Agent（Claude / OpenAI）的对话形态。
 *
 * 用户发一句话，后端驱动一个真正的 LLM tool-loop：
 *   agent_message（Agent 的思考/计划）-> tool_call（正在调用哪个工具）
 *   -> tool_result（工具结果）-> final（最终交付/PR 初稿）-> done
 * 本页把这些事件流式渲染成"对话 + 工具调用卡片"。
 */

import { useEffect, useMemo, useRef, useState, type ChangeEvent, type ReactNode } from 'react';
import { Alert, Avatar, Button, Input, Select, Typography, message } from 'antd';
import {
  ApiOutlined,
  ArrowUpOutlined,
  BookOutlined,
  CloudDownloadOutlined,
  ExportOutlined,
  FileAddOutlined,
  PaperClipOutlined,
  PlusOutlined,
  RobotOutlined,
  RocketOutlined,
  SaveOutlined,
  SafetyCertificateOutlined,
  SearchOutlined,
  StarOutlined,
  SyncOutlined,
  TagsOutlined,
  UnorderedListOutlined,
  UserOutlined,
  CloseCircleFilled,
  CheckCircleFilled,
  BarsOutlined,
} from '@ant-design/icons';
import {
  agentEngineApi,
  manuscriptApi,
  type AgentEngineEvent,
  type AgentEngineMsg,
  type AgentEngineThread,
  type Manuscript,
} from '../api/client';

/** 把时间字符串转成本地日期 YYYY-MM-DD（用于按日期筛选稿件） */
function toLocalDay(t?: string): string {
  if (!t) return '';
  const d = new Date(t);
  if (Number.isNaN(d.getTime())) return '';
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

const { Text, Paragraph, Title } = Typography;

/* ---- 设计变量 ---- */
const BRAND_GRADIENT = 'linear-gradient(135deg,#6366f1 0%,#8b5cf6 100%)';
const USER_GRADIENT = 'linear-gradient(135deg,#4f46e5 0%,#7c3aed 100%)';
const SOFT_BG = '#f6f7f9';
const CARD_BG = '#ffffff';
const BORDER = 'rgba(20,24,38,0.08)';
const TEXT = '#1a1d26';
const TEXT_WEAK = '#6b7280';

type ThinkingStep =
  | { type: 'text'; text: string }
  | {
      type: 'tool';
      id: string;
      name: string;
      args: Record<string, unknown>;
      result?: Record<string, unknown>;
      error?: string;
      status: 'running' | 'ok' | 'error';
    };

type PlanStep = {
  step_id: string;
  title: string;
  tools: string[];
  expected_output?: string;
  status: 'pending' | 'completed';
};

type Entry =
  | { kind: 'assistant'; text: string; thinking?: ThinkingStep[]; plan?: PlanStep[] }
  | { kind: 'final'; text: string }
  | {
      kind: 'tool';
      id: string;
      name: string;
      args: Record<string, unknown>;
      result?: Record<string, unknown>;
      error?: string;
      status: 'running' | 'ok' | 'error';
    };

/** 每个工具的展示名 / 主题色 / 图标。 */
function toolMeta(name: string): { label: string; color: string; icon: ReactNode } {
  const map: Record<string, { label: string; color: string; icon: ReactNode }> = {
    list_articles: { label: '梳理候选文章', color: '#6366f1', icon: <UnorderedListOutlined /> },
    get_article: { label: '阅读文章全文', color: '#0ea5e9', icon: <BookOutlined /> },
    search_news: { label: '搜索新闻', color: '#f59e0b', icon: <SearchOutlined /> },
    crawl_news: { label: '抓取最新新闻', color: '#10b981', icon: <CloudDownloadOutlined /> },
    classify_article: { label: '安全事件分类', color: '#8b5cf6', icon: <TagsOutlined /> },
    match_products: { label: '匹配关联产品', color: '#ec4899', icon: <ApiOutlined /> },
    score_article: { label: 'PR 价值评估', color: '#f59e0b', icon: <StarOutlined /> },
    generate_draft: { label: '生成 PR 初稿', color: '#6366f1', icon: <RocketOutlined /> },
    review_draft: {
      label: '合规与质量检查',
      color: '#10b981',
      icon: <SafetyCertificateOutlined />,
    },
    revise_draft: { label: '改写 / 润色稿件', color: '#0ea5e9', icon: <SyncOutlined /> },
    save_draft_version: { label: '保存历史版本', color: '#64748b', icon: <SaveOutlined /> },
    export_draft: { label: '导出稿件', color: '#334155', icon: <ExportOutlined /> },
  };
  const m = map[name];
  if (!m) return { label: name, color: '#6b7280', icon: <RobotOutlined /> };
  return m;
}

function chip(text: string, color: string, light = true): ReactNode {
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        padding: '2px 9px',
        borderRadius: 999,
        fontSize: 12,
        fontWeight: 500,
        color,
        background: light ? hexA(color, 0.12) : color,
        whiteSpace: 'nowrap',
      }}
    >
      {text}
    </span>
  );
}

/* ---- 显式执行计划卡片（形态 A：先出计划、逐步勾选） ---- */
function PlanCard({ steps }: { steps: PlanStep[] }) {
  const done = steps.filter((s) => s.status === 'completed').length;
  return (
    <div
      style={{
        maxWidth: 760,
        margin: '6px 0 0 46px',
        border: '1px solid rgba(99,102,241,0.28)',
        borderRadius: 12,
        background: 'rgba(99,102,241,0.03)',
        overflow: 'hidden',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '8px 12px',
          borderBottom: `1px solid ${BORDER}`,
          background: hexA('#6366f1', 0.06),
        }}
      >
        <span style={{ color: '#6366f1', fontSize: 13, display: 'inline-flex' }}>
          <BarsOutlined />
        </span>
        <span style={{ fontSize: 13, fontWeight: 600, color: TEXT }}>执行计划</span>
        <span style={{ marginLeft: 'auto', fontSize: 11.5, color: TEXT_WEAK }}>
          {done}/{steps.length} 已完成
        </span>
      </div>
      <div style={{ padding: '8px 12px 10px' }}>
        {steps.map((s, i) => {
          const completed = s.status === 'completed';
          const tools = s.tools.map((name) => toolMeta(name).label);
          return (
            <div
              key={s.step_id || `p-${i}`}
              style={{ display: 'flex', gap: 10, padding: '5px 2px', alignItems: 'flex-start' }}
            >
              <span
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  width: 18,
                  height: 18,
                  borderRadius: 999,
                  flex: 'none',
                  marginTop: 1,
                  fontSize: 11,
                  color: completed ? '#10b981' : '#6366f1',
                  background: completed ? hexA('#10b981', 0.12) : hexA('#6366f1', 0.1),
                }}
              >
                {completed ? '✓' : i + 1}
              </span>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 13, color: completed ? TEXT_WEAK : TEXT, lineHeight: 1.6 }}>
                  {s.title}
                  {s.expected_output ? (
                    <span style={{ color: TEXT_WEAK, fontSize: 12 }}> — {s.expected_output}</span>
                  ) : null}
                </div>
                {tools.length > 0 ? (
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 3 }}>
                    {tools.map((label, ti) => (
                      <span
                        key={`${s.step_id}-${ti}`}
                        style={{
                          fontSize: 11,
                          color: completed ? '#10b981' : '#7c6bf3',
                          background: completed ? hexA('#10b981', 0.08) : hexA('#6366f1', 0.08),
                          padding: '1px 8px',
                          borderRadius: 999,
                        }}
                      >
                        {label}
                      </span>
                    ))}
                  </div>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** 把 #hex 转成带透明度 rgba。 */
function hexA(hex: string, a: number): string {
  const h = hex.replace('#', '');
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${a})`;
}

/** 工具结果摘要：把后端 _summary 折叠成好看的芯片列表。 */
function Summary({ result }: { result: Record<string, unknown> }) {
  const s = result?._summary as Record<string, unknown> | undefined;
  if (!s) {
    const keys = Object.keys(result || {}).filter((k) => !k.startsWith('_'));
    return keys.length ? (
      <Text style={{ fontSize: 12.5, color: TEXT_WEAK }}>工具已返回结果</Text>
    ) : null;
  }
  const kind = s.kind as string;
  const items: ReactNode[] = [];

  if (kind === 'list') {
    const list = Array.isArray(s.items) ? (s.items as Array<Record<string, unknown>>) : [];
    items.push(chip(`候选 ${String(s.count ?? list.length)} 条`, '#6366f1'));
    list.slice(0, 3).forEach((it) =>
      items.push(
        <span key={`${Math.random()}`} style={{ fontSize: 12.5, color: TEXT }}>
          • {String(it.title ?? '')}
        </span>,
      ),
    );
  } else if (kind === 'article') {
    items.push(chip(s.found ? '已找到' : '未找到', s.found ? '#10b981' : '#ef4444'));
    items.push(<span style={{ fontSize: 12.5 }}>{String(s.title ?? '')}</span>);
  } else if (kind === 'classify') {
    items.push(chip(String(s.category ?? '—'), s.eligible ? '#10b981' : '#f59e0b'));
    items.push(chip(`置信度 ${(Number(s.confidence) * 100).toFixed(0)}%`, '#8b5cf6'));
  } else if (kind === 'match') {
    items.push(chip(String(s.outcome ?? ''), '#10b981'));
    (Array.isArray(s.candidates) ? (s.candidates as Array<Record<string, unknown>>) : []).forEach(
      (c) => items.push(chip(String(c.name ?? c.product_id), '#ec4899')),
    );
  } else if (kind === 'score') {
    items.push(
      chip(`总分 ${Number(s.total_score).toFixed(1)}`, s.worth_writing ? '#10b981' : '#f59e0b'),
    );
    items.push(chip(`产品相关 ${Number(s.product_relevance).toFixed(0)}`, '#6366f1'));
    items.push(chip(`事件影响 ${Number(s.event_impact).toFixed(0)}`, '#f59e0b'));
  } else if (kind === 'draft') {
    items.push(chip(`PR 初稿 #${String(s.version ?? '')}`, '#b45309', false));
    if (s.has_content) items.push(chip('已生成', '#10b981'));
    if (s.summary) items.push(<span style={{ fontSize: 12.5 }}>{String(s.summary)}</span>);
  } else if (kind === 'review') {
    items.push(chip(s.passed ? '检查通过' : '存在问题', s.passed ? '#10b981' : '#ef4444'));
  } else if (kind === 'crawl') {
    items.push(chip(`抓取 · ${String(s.status ?? '')}`, '#10b981'));
    items.push(chip(`新增 ${String(s.added ?? 0)} / 更新 ${String(s.updated ?? 0)}`, '#64748b'));
  }

  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>{items}</div>
  );
}

/** 工具调用卡片：带图标、状态、结果摘要。 */
function ToolCard({ entry }: { entry: Extract<Entry, { kind: 'tool' }> }) {
  const meta = toolMeta(entry.name);
  const running = entry.status === 'running';
  return (
    <div
      style={{
        display: 'flex',
        gap: 12,
        alignSelf: 'flex-start',
        maxWidth: 720,
      }}
    >
      <Avatar
        size={34}
        style={{
          background: hexA(meta.color, 0.12),
          color: meta.color,
          flex: 'none',
          marginTop: 2,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
        icon={<span style={{ fontSize: 16 }}>{meta.icon}</span>}
      />
      <div
        style={{
          background: CARD_BG,
          border: `1px solid ${BORDER}`,
          borderRadius: 14,
          padding: '10px 14px',
          boxShadow: '0 1px 2px rgba(16,24,40,0.04)',
          minWidth: 260,
          flex: 1,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Text strong style={{ fontSize: 13.5, color: TEXT }}>
            {meta.label}
          </Text>
          <span style={{ color: '#9aa0aa', fontSize: 11.5, fontFamily: 'monospace' }}>
            {entry.name}
          </span>
          <span style={{ flex: 1 }} />
          {running && (
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
              <span className="pulse-dot" style={{ background: meta.color }} />
              <span style={{ color: meta.color, fontSize: 12, fontWeight: 500 }}>执行中</span>
            </span>
          )}
          {entry.status === 'ok' && (
            <CheckCircleFilled style={{ color: '#10b981', fontSize: 16 }} />
          )}
          {entry.status === 'error' && (
            <CloseCircleFilled style={{ color: '#ef4444', fontSize: 16 }} />
          )}
        </div>
        {running && (
          <div style={{ marginTop: 8, fontSize: 12.5, color: TEXT_WEAK }}>
            Agent 正在调用该工具…
          </div>
        )}
        {entry.status === 'ok' && entry.result && (
          <div style={{ marginTop: 8, paddingTop: 8, borderTop: `1px solid ${BORDER}` }}>
            <Summary result={entry.result} />
          </div>
        )}
        {entry.status === 'error' && (
          <div style={{ marginTop: 8, fontSize: 12.5, color: '#ef4444' }}>{entry.error}</div>
        )}
      </div>
    </div>
  );
}

/* ---- 欢迎引导建议 ---- */
const SUGGESTIONS = [
  {
    title: '生成产品 PR 初稿',
    desc: '结合本周 AI 安全动态，选一篇相关新闻生成初稿',
    icon: <RocketOutlined />,
  },
  {
    title: '抓取最新新闻',
    desc: '抓取并汇总最新 AI 智能体安全新闻',
    icon: <CloudDownloadOutlined />,
  },
  {
    title: '匹配产品并打分',
    desc: '把某条新闻匹配到相关产品并做 PR 价值评估',
    icon: <StarOutlined />,
  },
];

export default function AgentChatPage() {
  const [threads, setThreads] = useState<AgentEngineThread[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [activeThread, setActiveThread] = useState<AgentEngineThread | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [thinking, setThinking] = useState<ThinkingStep[]>([]);
  const thinkingRef = useRef<ThinkingStep[]>([]);
  const [livePlan, setLivePlan] = useState<PlanStep[]>([]);
  const planRef = useRef<PlanStep[]>([]);
  const [input, setInput] = useState('');
  const [isGenerating, setIsGenerating] = useState(false);
  const [error, setError] = useState('');
  const [resumeHint, setResumeHint] = useState('');
  const [sidebarQuery, setSidebarQuery] = useState('');
  const [pendingApproval, setPendingApproval] = useState<{
    approvalId: string;
    tool: string;
    args: Record<string, unknown> | null;
  } | null>(null);
  const esRef = useRef<EventSource | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);
  const liveThinkingRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // 稿件库：可选为"当前附件"注入 Agent 上下文，用于对话改稿
  const [manuscripts, setManuscripts] = useState<Manuscript[]>([]);
  const [currentManuscript, setCurrentManuscript] = useState<Pick<
    Manuscript,
    'manuscript_id' | 'title'
  > | null>(null);
  // 按保存日期筛选（YYYY-MM-DD，空串表示不过滤）
  const [msFilterDate, setMsFilterDate] = useState('');

  // 稿件库下拉的数据：按保存时间倒序，并按所选日期（本地日期）过滤
  const manuscriptOptions = useMemo(() => {
    const list = manuscripts.filter((m) => {
      if (!msFilterDate) return true;
      return toLocalDay(m.created_at || m.updated_at) === msFilterDate;
    });
    return [...list].sort(
      (a, b) =>
        new Date(b.created_at || b.updated_at).getTime() -
        new Date(a.created_at || a.updated_at).getTime(),
    );
  }, [manuscripts, msFilterDate]);

  const activeMsg = useMemo(() => activeThread?.messages ?? [], [activeThread]);

  useEffect(() => {
    agentEngineApi
      .listThreads()
      .then((list) => setThreads(list ?? []))
      .catch((e) => setError(String(e?.response?.data?.detail || e?.message)));
    manuscriptApi
      .list()
      .then((list) => setManuscripts(list ?? []))
      .catch(() => {
        /* 稿件库列表加载失败不阻塞主流程 */
      });
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [entries, activeMsg, isGenerating, thinking]);

  // 实时思考面板：内容变化时自动滚动到底部，保持最新步骤可见
  useEffect(() => {
    const el = liveThinkingRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [thinking, isGenerating]);

  useEffect(() => () => esRef.current?.close(), []);

  useEffect(() => {
    if (!isGenerating) textRef.current?.focus();
  }, [isGenerating]);

  const closeES = () => {
    esRef.current?.close();
    esRef.current = null;
  };

  const ensureThread = async (): Promise<AgentEngineThread | null> => {
    if (activeThread) return activeThread;
    try {
      const t = await agentEngineApi.createThread();
      setActiveThread(t);
      setActiveId(t.thread_id);
      setThreads((prev) => [t, ...prev]);
      return t;
    } catch (e) {
      const err = e as { response?: { data?: { detail?: unknown } }; message?: unknown };
      setError(String(err?.response?.data?.detail ?? err?.message ?? '创建会话失败'));
      return null;
    }
  };

  const selectThread = async (id: string) => {
    closeES();
    setIsGenerating(false);
    setEntries([]);
    setThinking([]);
    thinkingRef.current = [];
    setActiveId(id);
    try {
      const t = await agentEngineApi.getThread(id);
      setActiveThread(t);
    } catch (e) {
      const err = e as { response?: { data?: { detail?: unknown } }; message?: unknown };
      setError(String(err?.response?.data?.detail ?? err?.message ?? '加载会话失败'));
    }
  };

  const onNewChat = async () => {
    closeES();
    setActiveThread(null);
    setActiveId(null);
    setEntries([]);
    setThinking([]);
    thinkingRef.current = [];
    planRef.current = [];
    setLivePlan([]);
    setIsGenerating(false);
    const t = await ensureThread();
    if (t) {
      setActiveId(t.thread_id);
      setThreads((prev) => (prev.some((x) => x.thread_id === t.thread_id) ? prev : [t, ...prev]));
    }
  };

  const handleEvent = (ev: AgentEngineEvent) => {
    const d = ev.data ?? {};
    const updateThinking = (fn: (prev: ThinkingStep[]) => ThinkingStep[]) => {
      thinkingRef.current = fn(thinkingRef.current);
      setThinking(thinkingRef.current);
    };
    const clearThinking = () => {
      thinkingRef.current = [];
      setThinking([]);
    };
    switch (ev.event_type) {
      case 'plan': {
        const rawSteps = (Array.isArray(d.steps) ? d.steps : []) as Array<Record<string, unknown>>;
        const steps: PlanStep[] = rawSteps.map((s) => ({
          step_id: String(s.step_id ?? ''),
          title: String(s.title ?? ''),
          tools: Array.isArray(s.tools) ? s.tools.map(String) : [],
          expected_output: String(s.expected_output ?? '') || undefined,
          status: String(s.status ?? 'pending') === 'completed' ? 'completed' : 'pending',
        }));
        planRef.current = steps;
        setLivePlan(steps);
        break;
      }
      case 'plan_step': {
        const sid = String(d.step_id ?? '');
        const done = String(d.status ?? '') === 'completed';
        const next = planRef.current.map((p) =>
          p.step_id === sid && done ? { ...p, status: 'completed' as const } : p,
        );
        planRef.current = next;
        setLivePlan(next);
        break;
      }
      case 'agent_message':
        updateThinking((prev) => [...prev, { type: 'text', text: String(d.content ?? '') }]);
        break;
      case 'final':
        setEntries((prev) => [
          ...prev,
          {
            kind: 'assistant',
            text: String(d.content ?? ''),
            thinking: thinkingRef.current,
            plan: planRef.current.length > 0 ? planRef.current : undefined,
          },
        ]);
        clearThinking();
        planRef.current = [];
        setLivePlan([]);
        break;
      case 'tool_call': {
        const id = String(d.id ?? Math.random());
        updateThinking((prev) => [
          ...prev,
          {
            type: 'tool',
            id,
            name: String(d.name ?? ''),
            args: (d.args as Record<string, unknown>) ?? {},
            status: 'running',
          },
        ]);
        break;
      }
      case 'tool_result': {
        const cid = String(d.id ?? '');
        updateThinking((prev) =>
          prev.map((e) =>
            e.type === 'tool' && e.id === cid
              ? { ...e, status: 'ok', result: (d.summary as Record<string, unknown>) ?? {} }
              : e,
          ),
        );
        break;
      }
      case 'tool_error': {
        const cid = String(d.id ?? '');
        updateThinking((prev) =>
          prev.map((e) =>
            e.type === 'tool' && (e.id === cid || !cid)
              ? { ...e, status: 'error', error: String(d.error ?? '') }
              : e,
          ),
        );
        break;
      }
      case 'approval_requested':
        setPendingApproval({
          approvalId: String(d.approval_id ?? ''),
          tool: String(d.tool ?? ''),
          args: (d.args as Record<string, unknown> | null) ?? {},
        });
        break;
      case 'approval_resolved':
        setPendingApproval(null);
        break;
      case 'interrupted':
        setIsGenerating(false);
        setResumeHint(
          '任务已中断，进度已保留。输入“继续”即可从中断处续跑；直接发其他内容将开启新一轮对话。',
        );
        break;
      case 'resumed':
        setResumeHint('');
        break;
      case 'error':
        setError(String(d.error ?? '生成过程出错'));
        setIsGenerating(false);
        break;
      default:
        break;
    }
  };

  const onSend = async (text?: string) => {
    const content = (text ?? input).trim();
    if (!content || isGenerating) return;
    setInput('');
    setError('');
    setResumeHint('');
    setThinking([]);
    thinkingRef.current = [];
    planRef.current = [];
    setLivePlan([]);
    setIsGenerating(true);

    const thread = await ensureThread();
    if (!thread) {
      setIsGenerating(false);
      return;
    }
    try {
      const updated = await agentEngineApi.sendMessage(
        thread.thread_id,
        content,
        currentManuscript?.manuscript_id,
      );
      setActiveThread(updated);
    } catch (e) {
      const err = e as { response?: { data?: { detail?: unknown } }; message?: unknown };
      setError(String(err?.response?.data?.detail ?? err?.message ?? '发送失败'));
      setIsGenerating(false);
      return;
    }

    closeES();
    const es = agentEngineApi.openEventSource(thread.thread_id, handleEvent, async () => {
      esRef.current = null;
      setIsGenerating(false);
      try {
        const t = await agentEngineApi.getThread(thread.thread_id);
        setActiveThread(t);
        setEntries([]);
        setThinking([]);
        thinkingRef.current = [];
        setThreads((prev) => {
          const rest = prev.filter((x) => x.thread_id !== t.thread_id);
          return [t, ...rest];
        });
      } catch {
        /* ignore */
      }
    });
    esRef.current = es;
  };

  const handleApproval = async (approved: boolean) => {
    const p = pendingApproval;
    if (!p) return;
    try {
      await agentEngineApi.resolveApproval(p.approvalId, approved);
    } catch (e) {
      const err = e as { response?: { data?: { detail?: unknown } }; message?: unknown };
      setError(String(err?.response?.data?.detail ?? err?.message ?? '审批提交失败'));
    }
    setPendingApproval(null);
  };

  const handleStop = async () => {
    if (!activeId) return;
    try {
      await agentEngineApi.stopGeneration(activeId);
      setResumeHint('正在停止并保存断点…');
    } catch (e) {
      const err = e as { response?: { data?: { detail?: unknown } }; message?: unknown };
      setError(String(err?.response?.data?.detail ?? err?.message ?? '停止失败'));
    }
  };

  // ── 稿件库操作 ───────────────────────────────────────────
  const refreshManuscripts = async () => {
    try {
      setManuscripts(await manuscriptApi.list());
    } catch {
      /* 忽略 */
    }
  };

  const saveManuscript = async (title: string, content: string, source = 'agent_generate') => {
    if (!content.trim()) {
      message.warning('没有可保存的稿件内容');
      return;
    }
    try {
      const m = await manuscriptApi.create({
        title: title || '未命名稿件',
        content_md: content,
        source,
      });
      await refreshManuscripts();
      setCurrentManuscript({ manuscript_id: m.manuscript_id, title: m.title });
      message.success(`已保存到稿件库「${m.title}」，可继续发送消息对它进行改稿`);
    } catch (e) {
      const err = e as { response?: { data?: { detail?: unknown } }; message?: unknown };
      setError(String(err?.response?.data?.detail ?? err?.message ?? '保存稿件失败'));
    }
  };

  const downloadTextAsMd = (title: string, content: string) => {
    if (!content.trim()) return;
    const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${(title || '稿件').replace(/[\\/:*?"<>|]/g, '_')}.md`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  const onPickFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (!/\.(md|txt|markdown)$/i.test(file.name)) {
      setError('仅支持 .md / .txt / .markdown 文本文件');
      e.target.value = '';
      return;
    }
    try {
      const text = await file.text();
      await saveManuscript(file.name.replace(/\.(md|txt|markdown)$/i, ''), text, 'manual');
    } catch (err) {
      const ex = err as { message?: unknown };
      setError(String(ex?.message ?? '读取文件失败'));
    } finally {
      e.target.value = '';
    }
  };

  const onSelectManuscript = async (id: string) => {
    if (!id) return;
    try {
      const m = await manuscriptApi.get(id);
      setCurrentManuscript({ manuscript_id: m.manuscript_id, title: m.title });
      message.info(`已添加稿件「${m.title}」，发送消息即可基于它进行改稿`);
    } catch (e) {
      const err = e as { response?: { data?: { detail?: unknown } }; message?: unknown };
      setError(String(err?.response?.data?.detail ?? err?.message ?? '加载稿件失败'));
    }
  };

  const onClearManuscript = () => {
    setCurrentManuscript(null);
    message.info('已移除当前稿件附件');
  };

  const filteredThreads = useMemo(() => {
    const q = sidebarQuery.trim().toLowerCase();
    if (!q) return threads;
    return threads.filter((t) => (t.title || '').toLowerCase().includes(q));
  }, [threads, sidebarQuery]);

  const groupedThreads = useMemo(() => {
    const buckets: Record<string, AgentEngineThread[]> = {
      今天: [],
      昨天: [],
      更早: [],
    };
    const now = new Date();
    const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const DAY = 86_400_000;
    for (const t of filteredThreads) {
      const ts = new Date(t.created_at).getTime();
      const key = ts >= startToday ? '今天' : ts >= startToday - DAY ? '昨天' : '更早';
      buckets[key].push(t);
    }
    return (['今天', '昨天', '更早'] as const)
      .filter((label) => buckets[label].length > 0)
      .map((label) => ({ label, items: buckets[label] }));
  }, [filteredThreads]);

  return (
    <div
      style={{
        display: 'flex',
        height: 'calc(100vh - 64px)',
        background: SOFT_BG,
        overflow: 'hidden',
      }}
    >
      <style>{`
        @keyframes pulse{
          0%,100%{opacity:1;transform:scale(1)}
          50%{opacity:.35;transform:scale(.7)}
        }
        .pulse-dot{width:8px;height:8px;border-radius:50%;animation:pulse 1.1s ease-in-out infinite}
        @keyframes typing{
          0%,60%,100%{transform:translateY(0);opacity:.4}
          30%{transform:translateY(-3px);opacity:1}
        }
      `}</style>

      {/* 历史会话栏 */}
      <aside
        style={{
          width: 272,
          flex: 'none',
          background: 'transparent',
          borderRight: `1px solid ${BORDER}`,
          display: 'flex',
          flexDirection: 'column',
          padding: '16px 10px 16px 20px',
        }}
      >
        <div style={{ paddingBottom: 14, display: 'flex', alignItems: 'center', gap: 10 }}>
          <div
            style={{
              width: 34,
              height: 34,
              borderRadius: 10,
              background: BRAND_GRADIENT,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: '#fff',
              fontSize: 16,
              boxShadow: '0 4px 12px rgba(99,102,241,0.35)',
              flex: 'none',
            }}
          >
            <RobotOutlined />
          </div>
          <div>
            <div style={{ fontWeight: 700, fontSize: 15, color: TEXT }}>历史记录</div>
            <div style={{ fontSize: 11.5, color: TEXT_WEAK }}>会话列表</div>
          </div>
        </div>

        {/* 新对话 */}
        <div style={{ paddingBottom: 12 }}>
          <Button
            type="primary"
            block
            icon={<PlusOutlined />}
            onClick={onNewChat}
            style={{
              height: 40,
              borderRadius: 12,
              background: BRAND_GRADIENT,
              border: 'none',
              boxShadow: '0 4px 12px rgba(99,102,241,0.3)',
              fontWeight: 600,
            }}
          >
            开始新对话
          </Button>
        </div>

        {/* 搜索 */}
        <div style={{ paddingBottom: 12 }}>
          <Input
            prefix={<SearchOutlined style={{ color: '#9aa0aa' }} />}
            placeholder="搜索会话…"
            value={sidebarQuery}
            onChange={(e) => setSidebarQuery(e.target.value)}
            allowClear
            variant="borderless"
            style={{
              background: CARD_BG,
              borderRadius: 12,
              border: `1px solid ${BORDER}`,
              boxShadow: '0 1px 2px rgba(16,24,40,0.04)',
            }}
          />
        </div>

        {/* 会话列表 */}
        <div style={{ flex: 1, overflowY: 'auto', paddingBottom: 12 }}>
          {groupedThreads.length === 0 ? (
            <div style={{ textAlign: 'center', padding: 36, color: TEXT_WEAK }}>
              <RobotOutlined style={{ fontSize: 26, color: '#c9cdd4' }} />
              <div style={{ fontSize: 12.5, marginTop: 8 }}>还没有会话，开始你的第一个对话吧</div>
            </div>
          ) : (
            groupedThreads.map((g) => (
              <div key={g.label}>
                <div
                  style={{
                    padding: '12px 10px 4px',
                    fontSize: 11.5,
                    color: TEXT_WEAK,
                    fontWeight: 600,
                    letterSpacing: 0.5,
                  }}
                >
                  {g.label}
                </div>
                {g.items.map((t) => {
                  const isActive = t.thread_id === activeId;
                  return (
                    <div
                      key={t.thread_id}
                      onClick={() => selectThread(t.thread_id)}
                      style={{
                        margin: '4px 0',
                        cursor: 'pointer',
                        border: `1px solid ${isActive ? 'rgba(99,102,241,0.35)' : BORDER}`,
                        borderRadius: 12,
                        padding: '10px 12px',
                        background: isActive ? '#ffffff' : 'rgba(255,255,255,0.6)',
                        boxShadow: isActive
                          ? '0 3px 12px rgba(99,102,241,0.16)'
                          : '0 1px 2px rgba(16,24,40,0.03)',
                        transition: 'all .15s ease',
                        display: 'flex',
                        alignItems: 'center',
                        gap: 10,
                      }}
                      onMouseEnter={(e) => {
                        const el = e.currentTarget as HTMLElement;
                        el.style.background = '#fff';
                        el.style.borderColor = 'rgba(99,102,241,0.35)';
                      }}
                      onMouseLeave={(e) => {
                        const el = e.currentTarget as HTMLElement;
                        el.style.background = isActive ? '#ffffff' : 'rgba(255,255,255,0.6)';
                        el.style.borderColor = isActive
                          ? 'rgba(99,102,241,0.35)'
                          : (BORDER as string);
                      }}
                    >
                      <Avatar
                        size={28}
                        style={{
                          background: isActive ? BRAND_GRADIENT : '#eef0f4',
                          color: isActive ? '#fff' : '#6b7280',
                          flex: 'none',
                          fontSize: 13,
                        }}
                        icon={<RobotOutlined />}
                      />
                      <Text
                        ellipsis
                        style={{
                          fontSize: 13,
                          color: isActive ? '#4f46e5' : TEXT,
                          fontWeight: isActive ? 600 : 400,
                        }}
                      >
                        {t.title || '新对话'}
                      </Text>
                    </div>
                  );
                })}
              </div>
            ))
          )}
        </div>

        {/* 底部 */}
        <div style={{ padding: '12px 8px', display: 'flex', alignItems: 'center', gap: 8 }}>
          <Avatar
            size={28}
            style={{ background: '#eef0f4', color: TEXT_WEAK }}
            icon={<UserOutlined />}
          />
          <Text style={{ fontSize: 12, color: TEXT_WEAK }}>智能体安全 PR 工作台</Text>
        </div>
      </aside>

      {/* 对话区 */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        {/* 顶栏 */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 12,
            padding: '12px 28px',
            background: 'rgba(255,255,255,0.85)',
            backdropFilter: 'blur(6px)',
            borderBottom: `1px solid ${BORDER}`,
          }}
        >
          <Title
            level={5}
            style={{
              margin: 0,
              fontWeight: 700,
              color: TEXT,
              flex: 1,
              display: 'flex',
              alignItems: 'center',
              gap: 8,
            }}
          >
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {activeThread?.title || '新的 Agent 对话'}
            </span>
          </Title>
          <div
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 6,
              padding: '3px 12px',
              borderRadius: 999,
              fontSize: 12,
              fontWeight: 500,
              color: isGenerating ? '#7c3aed' : '#10b981',
              background: isGenerating ? hexA('#8b5cf6', 0.1) : hexA('#10b981', 0.1),
            }}
          >
            <span
              className="pulse-dot"
              style={{ background: isGenerating ? '#8b5cf6' : '#10b981' }}
            />
            {isGenerating ? 'Agent 思考中' : '在线'}
          </div>
        </div>

        {/* 消息区 */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '24px 0 8px' }}>
          <div style={{ maxWidth: 820, margin: '0 auto', padding: '0 28px' }}>
            {activeMsg.length === 0 && entries.length === 0 && !isGenerating ? (
              <Welcome onPick={(t) => onSend(t)} />
            ) : (
              <>
                {activeMsg.map((m, idx) => (
                  <MessageRow
                    key={`p-${idx}`}
                    role={m.role}
                    msg={m}
                    onSave={saveManuscript}
                    onDownload={downloadTextAsMd}
                  />
                ))}
                {entries.map((entry, idx) =>
                  renderEntry(entry, idx, saveManuscript, downloadTextAsMd),
                )}
                {isGenerating && livePlan.length > 0 && <PlanCard steps={livePlan} />}
                {isGenerating && thinking.length > 0 && (
                  <div
                    style={{
                      maxWidth: 760,
                      margin: '6px 0 0 46px',
                      border: `1px solid ${BORDER}`,
                      borderRadius: 12,
                      background: 'rgba(255,255,255,0.55)',
                      display: 'flex',
                      flexDirection: 'column',
                      overflow: 'hidden',
                    }}
                  >
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 8,
                        padding: '8px 12px',
                        borderBottom: `1px solid ${BORDER}`,
                        background: hexA('#8b5cf6', 0.05),
                      }}
                    >
                      <span style={{ color: '#8b5cf6', fontSize: 13, display: 'inline-flex' }}>
                        <SyncOutlined spin />
                      </span>
                      <span style={{ fontSize: 13, fontWeight: 600, color: TEXT }}>思考过程</span>
                      <span
                        style={{
                          marginLeft: 'auto',
                          fontSize: 11.5,
                          color: TEXT_WEAK,
                        }}
                      >
                        {thinking.length} 步
                      </span>
                    </div>
                    <div
                      ref={liveThinkingRef}
                      style={{
                        maxHeight: 220,
                        overflowY: 'auto',
                        padding: '8px 12px 12px',
                      }}
                    >
                      {thinking.map((s, i) => {
                        if (s.type === 'text') {
                          return (
                            <div
                              key={`lt-${i}`}
                              style={{
                                fontSize: 13,
                                lineHeight: 1.7,
                                color: TEXT,
                                padding: '6px 2px 2px',
                                whiteSpace: 'pre-wrap',
                              }}
                            >
                              {s.text}
                            </div>
                          );
                        }
                        return (
                          <div
                            key={`lt-${i}`}
                            style={{ marginTop: 6, fontSize: 12, color: '#7c3aed' }}
                          >
                            ⚙ 调用工具：{s.name}
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}
                {isGenerating && (
                  <div
                    style={{ display: 'flex', gap: 12, alignItems: 'flex-start', marginTop: 14 }}
                  >
                    <Avatar
                      size={34}
                      style={{ background: BRAND_GRADIENT, color: '#fff', flex: 'none' }}
                      icon={<RobotOutlined />}
                    />
                    <div style={{ display: 'flex', gap: 4, alignItems: 'center', paddingTop: 8 }}>
                      {[0, 1, 2].map((i) => (
                        <span
                          key={i}
                          style={{
                            width: 6,
                            height: 6,
                            borderRadius: '50%',
                            background: '#7c3aed',
                            animation: `typing 1.2s ${i * 0.15}s infinite`,
                          }}
                        />
                      ))}
                    </div>
                  </div>
                )}
                <div ref={bottomRef} style={{ height: 8 }} />
              </>
            )}
          </div>
        </div>

        {error && (
          <div style={{ maxWidth: 820, margin: '0 auto', width: '100%', padding: '0 28px 6px' }}>
            <Alert
              type="error"
              showIcon
              message={error}
              closable
              onClose={() => setError('')}
              style={{ borderRadius: 12, border: '1px solid rgba(239,68,68,0.25)' }}
            />
          </div>
        )}

        {/* 输入区 */}
        <div style={{ padding: '10px 28px 22px' }}>
          {/* 稿件附件工具条：本地上传 / 从稿件库选择 -> 作为改稿上下文 */}
          <div style={{ maxWidth: 820, margin: '0 auto', width: '100%', paddingBottom: 10 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <span
                style={{
                  fontSize: 12.5,
                  color: TEXT_WEAK,
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 5,
                }}
              >
                <PaperClipOutlined /> 稿件附件
              </span>
              {currentManuscript ? (
                <span
                  style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 6,
                    padding: '3px 10px',
                    borderRadius: 999,
                    background: hexA('#10b981', 0.12),
                    color: '#059669',
                    fontSize: 12.5,
                    fontWeight: 500,
                    maxWidth: 260,
                  }}
                >
                  <span
                    style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                  >
                    {currentManuscript.title}
                  </span>
                  <CloseCircleFilled
                    onClick={onClearManuscript}
                    style={{ cursor: 'pointer', fontSize: 13, opacity: 0.7, flex: 'none' }}
                  />
                </span>
              ) : (
                <span style={{ fontSize: 12, color: '#9aa0aa' }}>
                  未添加（可用本地上传或从稿件库选择）
                </span>
              )}
              <div style={{ flex: 1 }} />
              <input
                ref={fileInputRef}
                type="file"
                accept=".md,.txt,.markdown"
                style={{ display: 'none' }}
                onChange={onPickFile}
              />
              <Button
                size="small"
                icon={<FileAddOutlined />}
                onClick={() => fileInputRef.current?.click()}
                style={{ borderRadius: 8, border: `1px solid ${BORDER}`, color: TEXT_WEAK }}
              >
                本地上传
              </Button>
              <input
                type="date"
                value={msFilterDate}
                onChange={(e) => setMsFilterDate(e.target.value)}
                title="按保存日期筛选"
                style={{
                  fontSize: 12,
                  padding: '3px 6px',
                  borderRadius: 8,
                  border: `1px solid ${BORDER}`,
                  color: TEXT_WEAK,
                  maxWidth: 132,
                }}
              />
              <Select
                size="small"
                placeholder="从稿件库选择"
                showSearch
                style={{ minWidth: 170 }}
                onChange={(v) => onSelectManuscript(v as string)}
                options={manuscriptOptions.map((m) => ({ label: m.title, value: m.manuscript_id }))}
                filterOption={(input, option) =>
                  String(option?.label ?? '')
                    .toLowerCase()
                    .includes(input.toLowerCase())
                }
                notFoundContent="稿件库为空，可先保存或上传一份"
              />
              {msFilterDate && (
                <Button
                  size="small"
                  type="text"
                  style={{ color: TEXT_WEAK, fontSize: 12 }}
                  onClick={() => setMsFilterDate('')}
                >
                  清除日期筛选
                </Button>
              )}
            </div>
          </div>
          {pendingApproval && (
            <div style={{ maxWidth: 820, margin: '0 auto', width: '100%', paddingBottom: 10 }}>
              <div
                style={{
                  background: `linear-gradient(135deg, ${hexA('#6366f1', 0.08)}, ${hexA('#8b5cf6', 0.08)})`,
                  border: '1px solid rgba(99,102,241,0.35)',
                  borderRadius: 14,
                  padding: '14px 16px',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <SafetyCertificateOutlined style={{ color: '#6366f1', fontSize: 18 }} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 600, fontSize: 14, color: TEXT }}>
                      Agent 请求执行「{pendingApproval.tool}」
                    </div>
                    <div style={{ fontSize: 12.5, color: '#6b7280', marginTop: 2 }}>
                      该操作带副作用，请确认是否允许执行。
                      {pendingApproval.args && Object.keys(pendingApproval.args).length > 0 && (
                        <span style={{ display: 'block', marginTop: 4 }}>
                          参数：{JSON.stringify(pendingApproval.args)}
                        </span>
                      )}
                    </div>
                  </div>
                  <Button
                    type="text"
                    danger
                    disabled={!isGenerating}
                    onClick={() => handleApproval(false)}
                    style={{ border: '1px solid rgba(239,68,68,0.3)', color: '#dc2626' }}
                  >
                    拒绝
                  </Button>
                  <Button
                    type="primary"
                    disabled={!isGenerating}
                    onClick={() => handleApproval(true)}
                    style={{ background: BRAND_GRADIENT, border: 'none' }}
                  >
                    批准执行
                  </Button>
                </div>
              </div>
            </div>
          )}
          {resumeHint && (
            <div style={{ maxWidth: 820, margin: '0 auto', width: '100%', paddingBottom: 10 }}>
              <Alert
                type={resumeHint.startsWith('正在') ? 'info' : 'warning'}
                showIcon
                message={resumeHint}
                closable
                onClose={() => setResumeHint('')}
                style={{ borderRadius: 12, border: '1px solid rgba(245,158,11,0.35)' }}
              />
            </div>
          )}
          <div
            style={{
              maxWidth: 820,
              margin: '0 auto',
              background: CARD_BG,
              border: `1.5px solid ${isGenerating ? hexA('#6366f1', 0.5) : BORDER}`,
              borderRadius: 18,
              boxShadow: '0 8px 30px rgba(16,24,40,0.08)',
              padding: '12px 14px',
              transition: 'border-color .2s ease',
            }}
          >
            <Input.TextArea
              ref={textRef as never}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onPressEnter={(e) => {
                if (!e.shiftKey) {
                  e.preventDefault();
                  onSend();
                }
              }}
              autoSize={{ minRows: 1, maxRows: 6 }}
              placeholder="给 Agent 下达 PR 任务，例如：结合最近 AI 安全新闻，选一篇匹配产品并生成初稿…"
              disabled={isGenerating}
              variant="borderless"
              style={{ fontSize: 14.5, padding: '2px 4px', color: TEXT }}
            />
            <div style={{ display: 'flex', alignItems: 'center', marginTop: 8, gap: 10 }}>
              <span style={{ fontSize: 11.5, color: '#9aa0aa' }}>回车发送 · Shift+回车换行</span>
              <div style={{ flex: 1 }} />
              {isGenerating ? (
                <Button
                  shape="circle"
                  onClick={handleStop}
                  title="停止生成（保留断点，可继续）"
                  style={{
                    width: 38,
                    height: 38,
                    border: '1px solid rgba(239,68,68,0.35)',
                    background: '#fff',
                    color: '#ef4444',
                    boxShadow: '0 2px 8px rgba(239,68,68,0.18)',
                  }}
                >
                  <CloseCircleFilled />
                </Button>
              ) : (
                <Button
                  shape="circle"
                  onClick={() => onSend()}
                  disabled={!input.trim()}
                  style={{
                    width: 38,
                    height: 38,
                    border: 'none',
                    background: input.trim() ? BRAND_GRADIENT : '#e5e7eb',
                    color: input.trim() ? '#fff' : '#9aa0aa',
                    boxShadow: input.trim() ? '0 4px 12px rgba(99,102,241,0.35)' : 'none',
                  }}
                >
                  <ArrowUpOutlined />
                </Button>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ---- 欢迎引导 ---- */
function Welcome({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div style={{ padding: '40px 8px 8px', textAlign: 'center' }}>
      <div
        style={{
          width: 72,
          height: 72,
          borderRadius: '50%',
          background: BRAND_GRADIENT,
          margin: '0 auto 18px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: '#fff',
          fontSize: 34,
          boxShadow: '0 12px 32px rgba(99,102,241,0.4)',
        }}
      >
        <RobotOutlined />
      </div>
      <Title level={3} style={{ margin: 0, fontWeight: 700, color: TEXT }}>
        今天想让 Agent 帮你做什么？
      </Title>
      <Paragraph style={{ color: TEXT_WEAK, marginTop: 6 }}>
        告诉它的目标，它会自主规划、调用工具、验证结果并交付 PR 初稿
      </Paragraph>

      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          gap: 10,
          marginTop: 26,
          maxWidth: 520,
          marginInline: 'auto',
        }}
      >
        {SUGGESTIONS.map((s) => (
          <button
            key={s.title}
            onClick={() => onPick(`${s.title}：${s.desc}`)}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 14,
              textAlign: 'left',
              padding: '13px 16px',
              borderRadius: 14,
              border: `1px solid ${BORDER}`,
              background: CARD_BG,
              cursor: 'pointer',
              transition: 'all .15s ease',
              boxShadow: '0 1px 2px rgba(16,24,40,0.04)',
            }}
            onMouseEnter={(e) => {
              (e.currentTarget as HTMLElement).style.borderColor = '#6366f1';
              (e.currentTarget as HTMLElement).style.boxShadow = '0 6px 18px rgba(99,102,241,0.15)';
              (e.currentTarget as HTMLElement).style.transform = 'translateY(-1px)';
            }}
            onMouseLeave={(e) => {
              (e.currentTarget as HTMLElement).style.borderColor = BORDER as string;
              (e.currentTarget as HTMLElement).style.boxShadow = '0 1px 2px rgba(16,24,40,0.04)';
              (e.currentTarget as HTMLElement).style.transform = 'translateY(0)';
            }}
          >
            <div
              style={{
                width: 38,
                height: 38,
                borderRadius: 11,
                background: hexA('#6366f1', 0.1),
                color: '#6366f1',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: 18,
                flex: 'none',
              }}
            >
              {s.icon}
            </div>
            <div>
              <div style={{ fontWeight: 600, fontSize: 14, color: TEXT }}>{s.title}</div>
              <div style={{ fontSize: 12.5, color: TEXT_WEAK }}>{s.desc}</div>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

/* ---- 消息行（用户 / Agent / 初稿卡片） ---- */
function MessageRow({
  role,
  msg,
  onSave,
  onDownload,
}: {
  role: string;
  msg: AgentEngineMsg;
  onSave?: (title: string, content: string) => void;
  onDownload?: (title: string, content: string) => void;
}) {
  if (role === 'user') {
    return (
      <div style={{ display: 'flex', justifyContent: 'flex-end', margin: '14px 0' }}>
        <div
          style={{
            background: USER_GRADIENT,
            color: '#fff',
            padding: '11px 16px',
            borderRadius: '16px 16px 4px 16px',
            maxWidth: 560,
            whiteSpace: 'pre-wrap',
            fontSize: 14.5,
            lineHeight: 1.6,
            boxShadow: '0 4px 16px rgba(79,70,229,0.28)',
          }}
        >
          {msg.content}
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', gap: 12, margin: '14px 0' }}>
      <Avatar
        size={34}
        style={{ background: BRAND_GRADIENT, color: '#fff', flex: 'none', marginTop: 2 }}
        icon={<RobotOutlined />}
      />
      <div style={{ flex: 1, minWidth: 0 }}>
        {msg.thinking && msg.thinking.length > 0 ? (
          <ThinkingBlock
            steps={msg.thinking.map((s) =>
              s.type === 'tool'
                ? {
                    type: 'tool',
                    id: `p-${s.name ?? ''}`,
                    name: s.name ?? '',
                    args: {},
                    status: 'ok' as const,
                  }
                : { type: 'text', text: s.text ?? '' },
            )}
          />
        ) : null}
        {msg.content && (
          <div
            style={{
              background: 'rgba(255,255,255,0.7)',
              padding: '11px 15px',
              borderRadius: 14,
              maxWidth: 720,
              whiteSpace: 'pre-wrap',
              fontSize: 14.5,
              lineHeight: 1.7,
              color: TEXT,
              marginTop: msg.thinking && msg.thinking.length > 0 ? 10 : 0,
            }}
          >
            {msg.content}
          </div>
        )}
        {msg.draft?.content ? (
          <DraftCard heading={msg.draft.heading || 'PR 初稿'} content={msg.draft.content} />
        ) : null}
        <ManuscriptActions
          title={(msg.draft?.heading || '稿件').replace(/\s+/g, ' ').trim()}
          content={msg.content || msg.draft?.content || ''}
          onSave={onSave}
          onDownload={onDownload}
        />
      </div>
    </div>
  );
}

function DraftCard({ heading, content }: { heading: string; content: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div
      style={{
        marginTop: 10,
        maxWidth: 760,
        background: 'linear-gradient(180deg,#fffdf6,#fff)',
        border: '1px solid rgba(217,164,0,0.35)',
        borderRadius: 14,
        overflow: 'hidden',
        boxShadow: '0 2px 12px rgba(217,164,0,0.08)',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '10px 14px',
          background: 'rgba(217,164,0,0.08)',
          cursor: 'pointer',
          userSelect: 'none',
        }}
        onClick={() => setOpen((v) => !v)}
      >
        <span
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            padding: '1px 9px',
            borderRadius: 999,
            background: '#b45309',
            color: '#fff',
            fontSize: 12,
            fontWeight: 600,
          }}
        >
          {heading}
        </span>
        <span style={{ fontSize: 12.5, color: '#92600a', fontWeight: 500 }}>
          点击{open ? '收起' : '展开'}查看完整初稿
        </span>
        <span style={{ flex: 1 }} />
        <span
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            width: 20,
            height: 20,
            borderRadius: 6,
            background: 'rgba(180,83,9,0.12)',
            color: '#b45309',
            fontSize: 12,
            transition: 'transform .2s ease',
            transform: open ? 'rotate(180deg)' : 'none',
          }}
        >
          ▾
        </span>
      </div>
      {open && (
        <pre
          style={{
            whiteSpace: 'pre-wrap',
            fontFamily: 'inherit',
            fontSize: 13.5,
            lineHeight: 1.75,
            color: '#323232',
            padding: '14px 16px',
            margin: 0,
            maxHeight: 460,
            overflow: 'auto',
          }}
        >
          {content}
        </pre>
      )}
    </div>
  );
}

/* ---- 思考过程（可折叠） ---- */
function ThinkingBlock({ steps }: { steps: ThinkingStep[] }) {
  const [open, setOpen] = useState(false);
  const toolCount = steps.filter((s) => s.type === 'tool').length;
  const lastText = [...steps].reverse().find((s) => s.type === 'text') as
    | { type: 'text'; text: string }
    | undefined;
  return (
    <div
      style={{
        maxWidth: 760,
        marginRight: 8,
        border: `1px solid ${BORDER}`,
        borderRadius: 12,
        background: 'rgba(255,255,255,0.55)',
        overflow: 'hidden',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '8px 12px',
          cursor: 'pointer',
          userSelect: 'none',
        }}
        onClick={() => setOpen((v) => !v)}
      >
        <span style={{ color: '#8b5cf6', fontSize: 13, display: 'inline-flex' }}>
          <SyncOutlined spin={!open} />
        </span>
        <span style={{ fontSize: 13, fontWeight: 600, color: TEXT }}>思考过程</span>
        {toolCount > 0 ? (
          <span
            style={{
              padding: '1px 8px',
              borderRadius: 999,
              background: hexA('#8b5cf6', 0.1),
              color: '#7c3aed',
              fontSize: 11.5,
              fontWeight: 500,
            }}
          >
            调用 {toolCount} 个工具
          </span>
        ) : null}
        {!open ? (
          <span
            style={{
              flex: 1,
              fontSize: 12,
              color: TEXT_WEAK,
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              paddingRight: 8,
            }}
          >
            {lastText ? `思考：${lastText.text}` : '已执行分析步骤'}
          </span>
        ) : (
          <span style={{ flex: 1 }} />
        )}
        <span
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            width: 20,
            height: 20,
            borderRadius: 6,
            background: 'rgba(139,92,246,0.1)',
            color: '#7c3aed',
            fontSize: 12,
            transition: 'transform .2s ease',
            transform: open ? 'rotate(180deg)' : 'none',
          }}
        >
          ▾
        </span>
      </div>
      {open && (
        <div style={{ padding: '4px 12px 12px', borderTop: `1px solid ${BORDER}` }}>
          {steps.map((s, i) => {
            if (s.type === 'text') {
              return (
                <div
                  key={`t-${i}`}
                  style={{
                    fontSize: 13,
                    lineHeight: 1.7,
                    color: TEXT,
                    padding: '8px 2px 2px',
                    whiteSpace: 'pre-wrap',
                  }}
                >
                  {s.text}
                </div>
              );
            }
            const tool = s as Extract<ThinkingStep, { type: 'tool' }>;
            return (
              <div key={`t-${i}`} style={{ marginTop: 8 }}>
                <ToolCard
                  entry={{
                    kind: 'tool' as const,
                    id: tool.id,
                    name: tool.name,
                    args: tool.args,
                    result: tool.result,
                    error: tool.error,
                    status: tool.status,
                  }}
                />
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/* ---- 流式条目渲染 ---- */
function renderEntry(
  entry: Entry,
  idx: number,
  onSave?: (title: string, content: string) => void,
  onDownload?: (title: string, content: string) => void,
) {
  if (entry.kind === 'assistant') {
    return (
      <div key={`e-${idx}`} style={{ display: 'flex', gap: 12, margin: '14px 0' }}>
        <Avatar
          size={34}
          style={{ background: BRAND_GRADIENT, color: '#fff', flex: 'none', marginTop: 2 }}
          icon={<RobotOutlined />}
        />
        <div style={{ flex: 1, minWidth: 0 }}>
          {entry.plan && entry.plan.length > 0 ? <PlanCard steps={entry.plan} /> : null}
          {entry.thinking && entry.thinking.length > 0 ? (
            <ThinkingBlock steps={entry.thinking} />
          ) : null}
          <div
            style={{
              maxWidth: 760,
              padding: '13px 16px',
              borderRadius: 14,
              border: '1px solid rgba(99,102,241,0.22)',
              background:
                entry.thinking && entry.thinking.length > 0
                  ? 'linear-gradient(180deg,#f4f5ff,#fff)'
                  : 'rgba(255,255,255,0.7)',
              fontSize: 14.5,
              lineHeight: 1.7,
              color: TEXT,
              marginTop: entry.thinking && entry.thinking.length > 0 ? 10 : 0,
            }}
          >
            <Paragraph style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{entry.text}</Paragraph>
          </div>
          <ManuscriptActions
            title="PR 稿件"
            content={entry.text}
            onSave={onSave}
            onDownload={onDownload}
          />
        </div>
      </div>
    );
  }
  if (entry.kind === 'final') {
    return (
      <div key={`e-${idx}`} style={{ display: 'flex', gap: 12, margin: '14px 0' }}>
        <Avatar
          size={34}
          style={{ background: BRAND_GRADIENT, color: '#fff', flex: 'none', marginTop: 2 }}
          icon={<RocketOutlined />}
        />
        <div
          style={{
            maxWidth: 780,
            padding: '13px 16px',
            borderRadius: 14,
            border: '1px solid rgba(99,102,241,0.22)',
            background: 'linear-gradient(180deg,#f4f5ff,#fff)',
            fontSize: 14.5,
            lineHeight: 1.7,
            color: TEXT,
          }}
        >
          <Paragraph style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{entry.text}</Paragraph>
          <ManuscriptActions
            title="稿件"
            content={entry.text}
            onSave={onSave}
            onDownload={onDownload}
            inside
          />
        </div>
      </div>
    );
  }
  return (
    <div
      key={`e-${idx}`}
      style={{
        margin: '12px 0',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'flex-start',
      }}
    >
      <ToolCard entry={entry} />
    </div>
  );
}

/** 生成稿件的操作栏：保存到稿件库 / 下载 md。 */
function ManuscriptActions({
  title,
  content,
  onSave,
  onDownload,
  inside = false,
}: {
  title: string;
  content: string;
  onSave?: (title: string, content: string) => void;
  onDownload?: (title: string, content: string) => void;
  inside?: boolean;
}) {
  if (!onSave && !onDownload) return null;
  if (!content?.trim()) return null;
  return (
    <div
      style={{
        display: 'flex',
        gap: 8,
        marginTop: inside ? 12 : 10,
        flexWrap: 'wrap',
        alignItems: 'center',
      }}
    >
      <Button
        size="small"
        icon={<SaveOutlined />}
        onClick={() => onSave?.(title, content)}
        style={{ borderRadius: 8, color: '#6366f1', border: '1px solid rgba(99,102,241,0.35)' }}
      >
        保存到稿件库
      </Button>
      <Button
        size="small"
        icon={<CloudDownloadOutlined />}
        onClick={() => onDownload?.(title, content)}
        style={{ borderRadius: 8, color: '#0ea5e9', border: '1px solid rgba(14,165,233,0.35)' }}
      >
        下载 md
      </Button>
      <span style={{ fontSize: 11.5, color: '#9aa0aa' }}>
        保存后可直接在下方「稿件附件」中继续发送消息进行对话改稿
      </span>
    </div>
  );
}
