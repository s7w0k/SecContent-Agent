/**
 * 对话改稿工作台页面
 *
 * 左栏：文章选择 + 草稿选择 + 原稿/修订稿预览 + 修订记录列表
 * 右栏：消息列表 + 输入框 + 模式切换（问答/改稿）
 */

import {
  ClearOutlined,
  CopyOutlined,
  DownloadOutlined,
  EditOutlined,
  ExpandAltOutlined,
  QuestionCircleOutlined,
  SendOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Button,
  Card,
  Collapse,
  Drawer,
  Empty,
  Input,
  Layout,
  Radio,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
  message,
} from 'antd';
import { useCallback, useEffect, useRef, useState } from 'react';
import type {
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
} from 'react';
import ReactMarkdown from 'react-markdown';
import api, { chatApi } from '../api/client';
import ChatBubble from '../components/ChatBubble';
import DraftBlockView, { type DraftBlock } from '../components/DraftBlockView';
import DraftFeedback from '../components/DraftFeedback';
import DraftReviewPanel from '../components/DraftReviewPanel';
import RevisionList from '../components/RevisionList';
import type {
  Article,
  ChatMessage,
  DraftReview,
  DraftReviseResponse,
  DraftRevision,
} from '../types';
import styles from './ChatPage.module.css';

const { Sider, Content } = Layout;
const { Text } = Typography;
const { TextArea } = Input;

type ChatMode = '问答' | '改稿';

const SIDER_WIDTH_STORAGE_KEY = 'chat-page-sider-width';
const DEFAULT_SIDER_WIDTH = 760;
const MIN_SIDER_WIDTH = 420;
const MIN_CONTENT_WIDTH = 280;
const RESIZE_HANDLE_WIDTH = 10;
const STACKED_BREAKPOINT = 992;

function getMaximumSiderWidth(layoutWidth?: number) {
  const availableWidth =
    layoutWidth && layoutWidth > 0
      ? layoutWidth
      : typeof window !== 'undefined'
        ? window.innerWidth
        : DEFAULT_SIDER_WIDTH + MIN_CONTENT_WIDTH + RESIZE_HANDLE_WIDTH;
  return Math.max(
    MIN_SIDER_WIDTH,
    availableWidth - MIN_CONTENT_WIDTH - RESIZE_HANDLE_WIDTH,
  );
}

function clampSiderWidth(width: number, layoutWidth?: number) {
  return Math.round(
    Math.min(Math.max(width, MIN_SIDER_WIDTH), getMaximumSiderWidth(layoutWidth)),
  );
}

function getInitialSiderWidth() {
  if (typeof window === 'undefined') return DEFAULT_SIDER_WIDTH;
  const storedWidth = Number(window.localStorage.getItem(SIDER_WIDTH_STORAGE_KEY));
  return clampSiderWidth(Number.isFinite(storedWidth) && storedWidth > 0 ? storedWidth : DEFAULT_SIDER_WIDTH);
}

const modeOptions = [
  { value: '问答', label: '问答', icon: <QuestionCircleOutlined /> },
  { value: '改稿', label: '改稿', icon: <EditOutlined /> },
];

function getRequestErrorMessage(error: unknown, fallback: string): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } }).response;
    if (typeof response?.data?.detail === 'string') return response.data.detail;
  }
  return error instanceof Error ? error.message : fallback;
}

export default function ChatPage() {
  const [siderWidth, setSiderWidth] = useState(getInitialSiderWidth);
  const [resizing, setResizing] = useState(false);

  // ── 文章 & 草稿 ──────────────────────────────────────────
  const [articles, setArticles] = useState<Article[]>([]);
  const [articlesLoading, setArticlesLoading] = useState(true);
  const [selectedArticle, setSelectedArticle] = useState<Article | null>(null);
  const [draftIndex, setDraftIndex] = useState<number>(0);

  // ── 对话 ─────────────────────────────────────────────────
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [mode, setMode] = useState<ChatMode>('问答');
  const [sending, setSending] = useState(false);

  // ── 修订稿预览 ───────────────────────────────────────────
  const [revisionResult, setRevisionResult] = useState<DraftReviseResponse | null>(null);
  const [viewingRevision, setViewingRevision] = useState<DraftRevision | null>(null);
  const [draftFullscreen, setDraftFullscreen] = useState(false);

  // ── 段落选中 ─────────────────────────────────────────────
  const [selectedBlock, setSelectedBlock] = useState<DraftBlock | null>(null);
  const handleSelectBlock = useCallback((block: DraftBlock | null) => {
    setSelectedBlock(block);
    if (block) setMode('改稿');
  }, []);

  // ── 应用修订 ─────────────────────────────────────────────
  const [applying, setApplying] = useState(false);
  const [reviewing, setReviewing] = useState(false);

  // ── 错误 ─────────────────────────────────────────────────
  const [error, setError] = useState<string | null>(null);

  // ── 历史加载 ─────────────────────────────────────────────
  const [historyLoading, setHistoryLoading] = useState(false);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const layoutRef = useRef<HTMLDivElement>(null);
  const resizeStartRef = useRef<{ pointerX: number; width: number } | null>(null);
  const resizeWidthRef = useRef(siderWidth);

  const getLayoutWidth = useCallback(
    () => layoutRef.current?.getBoundingClientRect().width || window.innerWidth,
    [],
  );

  const persistSiderWidth = useCallback((width: number) => {
    window.localStorage.setItem(SIDER_WIDTH_STORAGE_KEY, String(width));
  }, []);

  const applySiderWidth = (handle: HTMLDivElement, width: number) => {
    const sider = handle.previousElementSibling as HTMLElement | null;
    if (!sider) return;
    const cssWidth = `${width}px`;
    sider.style.flex = `0 0 ${cssWidth}`;
    sider.style.width = cssWidth;
    sider.style.minWidth = cssWidth;
    sider.style.maxWidth = cssWidth;
    handle.setAttribute('aria-valuenow', String(width));
  };

  const handleResizeStart = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || window.innerWidth < STACKED_BREAKPOINT) return;
    event.preventDefault();
    resizeStartRef.current = { pointerX: event.clientX, width: siderWidth };
    resizeWidthRef.current = siderWidth;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    setResizing(true);
  };

  const handleResizeMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const start = resizeStartRef.current;
    if (!start) return;
    const nextWidth = clampSiderWidth(
      start.width + event.clientX - start.pointerX,
      getLayoutWidth(),
    );
    resizeWidthRef.current = nextWidth;
    applySiderWidth(event.currentTarget, nextWidth);
  };

  const handleResizeEnd = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!resizeStartRef.current) return;
    resizeStartRef.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    setResizing(false);
    setSiderWidth(resizeWidthRef.current);
    persistSiderWidth(resizeWidthRef.current);
  };

  const handleResizeKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    let nextWidth: number | null = null;
    if (event.key === 'ArrowLeft') nextWidth = siderWidth - 20;
    if (event.key === 'ArrowRight') nextWidth = siderWidth + 20;
    if (event.key === 'Home') nextWidth = MIN_SIDER_WIDTH;
    if (event.key === 'End') nextWidth = getMaximumSiderWidth(getLayoutWidth());
    if (nextWidth === null) return;
    event.preventDefault();
    const clampedWidth = clampSiderWidth(nextWidth, getLayoutWidth());
    setSiderWidth(clampedWidth);
    persistSiderWidth(clampedWidth);
  };

  const resetSiderWidth = () => {
    const defaultWidth = clampSiderWidth(DEFAULT_SIDER_WIDTH, getLayoutWidth());
    setSiderWidth(defaultWidth);
    persistSiderWidth(defaultWidth);
  };

  // ── 加载有草稿的文章列表 ──────────────────────────────────
  const loadArticles = useCallback(async () => {
    setArticlesLoading(true);
    try {
      const resp = await api.getArticles({ page: 1, page_size: 100 });
      const withDrafts = resp.items.filter((a) => a.pr_drafts && a.pr_drafts.length > 0);
      setArticles(withDrafts);
    } catch {
      setError('加载文章列表失败');
    } finally {
      setArticlesLoading(false);
    }
  }, []);

  useEffect(() => {
    loadArticles();
  }, [loadArticles]);

  // ── 加载对话历史 ──────────────────────────────────────────
  const loadChatHistory = useCallback(async (urlHash: string, dIndex: number) => {
    setHistoryLoading(true);
    try {
      const history = await chatApi.getChatHistory(urlHash, dIndex);
      setMessages(history);
    } catch {
      setMessages([]);
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  // ── 消息列表滚动到底部 ────────────────────────────────────
  // biome-ignore lint/correctness/useExhaustiveDependencies: 消息内容变化时需要触发滚动。
  useEffect(() => {
    const container = messagesContainerRef.current;
    if (container) {
      container.scrollTop = container.scrollHeight;
    }
  }, [messages]);

  // ── 选择文章 ─────────────────────────────────────────────
  const handleArticleChange = (urlHash: string) => {
    const article = articles.find((a) => a.url_hash === urlHash) || null;
    setSelectedArticle(article);
    setDraftIndex(0);
    setRevisionResult(null);
    setViewingRevision(null);
    setDraftFullscreen(false);
    setError(null);
    setSelectedBlock(null);
    // 加载新文章+草稿0的对话历史
    if (article) {
      loadChatHistory(urlHash, 0);
    } else {
      setMessages([]);
    }
  };

  // ── 选择草稿 ─────────────────────────────────────────────
  const handleDraftChange = (index: number) => {
    setDraftIndex(index);
    setRevisionResult(null);
    setViewingRevision(null);
    setDraftFullscreen(false);
    setSelectedBlock(null);
    // 加载新草稿的对话历史
    if (selectedArticle) {
      loadChatHistory(selectedArticle.url_hash, index);
    }
  };

  // ── 发送消息 ─────────────────────────────────────────────
  const handleSend = async () => {
    const text = input.trim();
    if (!text) return;

    if (mode === '改稿' && !selectedArticle) {
      message.warning('改稿模式需要先选择文章和草稿');
      return;
    }

    const userMsg: ChatMessage = { role: 'user', content: text };
    const newMessages = [...messages, userMsg];
    setMessages(newMessages);
    setInput('');
    setSending(true);
    setError(null);

    try {
      if (mode === '问答') {
        // 流式问答：先插入空 assistant 消息，逐 chunk 更新
        const assistantIdx = newMessages.length;
        setMessages([...newMessages, { role: 'assistant', content: '' }]);

        let firstChunk = true;
        await chatApi.askStream(
          {
            message: text,
            article_url_hash: selectedArticle?.url_hash,
            draft_index: selectedArticle ? draftIndex : undefined,
            history: messages.length > 0 ? messages : undefined,
          },
          (chunk) => {
            if (firstChunk) {
              firstChunk = false;
            }
            setMessages((prev) => {
              const updated = [...prev];
              if (updated[assistantIdx]) {
                updated[assistantIdx] = {
                  ...updated[assistantIdx],
                  content: updated[assistantIdx].content + chunk,
                };
              }
              return updated;
            });
          },
          (_fullAnswer) => {
            // 流结束，answer 已通过 chunk 逐步拼接
          },
          (errMsg) => {
            setError(errMsg);
            setMessages((prev) => {
              const updated = [...prev];
              if (updated[assistantIdx]) {
                updated[assistantIdx] = {
                  ...updated[assistantIdx],
                  content: updated[assistantIdx].content || `错误：${errMsg}`,
                };
              }
              return updated;
            });
          },
        );
      } else {
        // 流式改稿：先插入空 assistant 消息，逐 chunk 更新
        const assistantIdx = newMessages.length;
        setMessages([...newMessages, { role: 'assistant', content: '' }]);

        const articleForRevise = selectedArticle;
        if (!articleForRevise) return;

        await chatApi.reviseDraftStream(
          articleForRevise.url_hash,
          draftIndex,
          {
            instruction: text,
            save: true,
            ...(selectedBlock
              ? {
                  selected_text: selectedBlock.text,
                  selected_range: { start: selectedBlock.index, end: selectedBlock.index },
                }
              : {}),
          },
          (chunk) => {
            setMessages((prev) => {
              const updated = [...prev];
              if (updated[assistantIdx]) {
                updated[assistantIdx] = {
                  ...updated[assistantIdx],
                  content: updated[assistantIdx].content + chunk,
                };
              }
              return updated;
            });
          },
          (result) => {
            setRevisionResult(result);
            setViewingRevision(null);
            message.success('修订稿已生成并保存');
            refreshArticle();
          },
          (errMsg) => {
            setError(errMsg);
            setMessages((prev) => {
              const updated = [...prev];
              if (updated[assistantIdx]) {
                updated[assistantIdx] = {
                  ...updated[assistantIdx],
                  content: updated[assistantIdx].content || `错误：${errMsg}`,
                };
              }
              return updated;
            });
          },
        );
      }
    } catch (err: unknown) {
      const errMsg = getRequestErrorMessage(err, '请求失败');
      setError(errMsg);
      setMessages([...newMessages, { role: 'assistant', content: `错误：${errMsg}` }]);
    } finally {
      setSending(false);
    }
  };

  // ── 刷新文章数据 ──────────────────────────────────────────
  const refreshArticle = async () => {
    if (!selectedArticle) return;
    try {
      const updated = await api.getArticle(selectedArticle.url_hash);
      setSelectedArticle(updated);
      setArticles((prev) => prev.map((a) => (a.url_hash === updated.url_hash ? updated : a)));
    } catch {
      // 刷新失败不阻塞流程
    }
  };

  // ── 选择查看某条修订记录 ──────────────────────────────────
  const handleSelectRevision = (rev: DraftRevision) => {
    setViewingRevision(rev);
    setRevisionResult(null);
  };

  // ── 应用修订 ─────────────────────────────────────────────
  const handleApplyRevision = async (rev: DraftRevision) => {
    if (!selectedArticle) return;
    setApplying(true);
    try {
      await chatApi.applyRevision(selectedArticle.url_hash, draftIndex, rev.revision_id);
      message.success('修订已应用为当前稿');
      await refreshArticle();
      setViewingRevision(null);
      setRevisionResult(null);
    } catch (err: unknown) {
      const errMsg = getRequestErrorMessage(err, '应用失败');
      message.error(errMsg);
    } finally {
      setApplying(false);
    }
  };

  const handleReviewDraft = async () => {
    if (!selectedArticle || !currentDraft) return;
    setReviewing(true);
    try {
      const review = await chatApi.reviewDraft(selectedArticle.url_hash, draftIndex);
      const withReview = (article: Article): Article => ({
        ...article,
        pr_drafts: article.pr_drafts?.map((draft, index) =>
          index === draftIndex ? { ...draft, review: review as DraftReview } : draft,
        ),
      });
      setSelectedArticle((current) => (current ? withReview(current) : current));
      setArticles((current) =>
        current.map((article) =>
          article.url_hash === selectedArticle.url_hash ? withReview(article) : article,
        ),
      );
      message.success('稿件检查完成');
    } catch (err: unknown) {
      message.error(getRequestErrorMessage(err, '稿件检查失败'));
    } finally {
      setReviewing(false);
    }
  };

  // ── 复制修订稿 ────────────────────────────────────────────
  const handleCopy = () => {
    const content = previewContent;
    if (!content) return;
    navigator.clipboard
      .writeText(content)
      .then(() => message.success('已复制'))
      .catch(() => message.error('复制失败'));
  };

  // ── 下载修订稿 ────────────────────────────────────────────
  const handleDownload = () => {
    const content = previewContent;
    const revId = viewingRevision?.revision_id || revisionResult?.revision_id || 'original';
    if (!content) return;
    const blob = new Blob([content], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `PR-revision-${revId.slice(0, 8)}.md`;
    a.click();
    URL.revokeObjectURL(url);
    if (selectedArticle && currentDraft) {
      void api
        .log({
          action: 'draft_download',
          target: {
            article_url_hash: selectedArticle.url_hash,
            draft_index: draftIndex,
            template: currentDraft.template,
            template_id: currentDraft.template_id,
            template_key: currentDraft.template_key,
            template_version: currentDraft.template_version,
            template_name: currentDraft.template,
            perspective: currentDraft.perspective,
            revision_id: viewingRevision?.revision_id || revisionResult?.revision_id,
          },
          context: {
            article_title: selectedArticle.title,
            category_v2: selectedArticle.category_v2,
            pr_total_score: selectedArticle.pr_total_score,
            source: 'chat_revision_preview',
          },
        })
        .catch(() => undefined);
    }
  };

  // ── 清空对话历史 ──────────────────────────────────────────
  const handleClearHistory = async () => {
    if (!selectedArticle) return;
    try {
      await chatApi.clearChatHistory(selectedArticle.url_hash, draftIndex);
      setMessages([]);
      message.success('对话历史已清空');
    } catch {
      message.error('清空失败');
    }
  };

  // ── 当前草稿 ─────────────────────────────────────────────
  const currentDraft = selectedArticle?.pr_drafts?.[draftIndex];
  const revisions: DraftRevision[] = currentDraft?.revisions || [];

  // ── 预览内容 ─────────────────────────────────────────────
  const previewContent =
    viewingRevision?.content_md ||
    revisionResult?.revised_content_md ||
    currentDraft?.content_md ||
    '';
  const previewTitle = viewingRevision ? '修订稿预览' : revisionResult ? '修订稿预览' : '原稿预览';

  const feedbackRevisionId =
    viewingRevision?.revision_id ||
    (revisionResult?.saved ? revisionResult.revision_id : undefined);

  return (
    <>
      <Layout ref={layoutRef} className={`${styles.layout} ${resizing ? styles.resizing : ''}`}>
        {/* ── 左栏：选择 + 预览 + 修订记录 ── */}
        <Sider width={siderWidth} className={styles.sider}>
          <Collapse
            ghost
            className={styles.selectorCollapse}
            defaultActiveKey={['article-selection', 'draft-selection']}
            items={[
              {
                key: 'article-selection',
                label: (
                  <Text strong className={styles.cardHeader}>
                    文章选择
                  </Text>
                ),
                children: articlesLoading ? (
                  <div className={styles.loadingIndicator}>
                    <Spin size="small" />
                    <span>加载中...</span>
                  </div>
                ) : (
                  <Select
                    showSearch
                    aria-label="选择有草稿的文章"
                    placeholder="选择有草稿的文章"
                    style={{ width: '100%' }}
                    value={selectedArticle?.url_hash}
                    onChange={handleArticleChange}
                    options={articles.map((a) => ({
                      label: a.title?.slice(0, 50),
                      value: a.url_hash,
                    }))}
                    optionFilterProp="label"
                    size="small"
                  />
                ),
              },
              ...(selectedArticle && currentDraft
                ? [
                    {
                      key: 'draft-selection',
                      label: (
                        <Text strong className={styles.cardHeader}>
                          草稿选择
                        </Text>
                      ),
                      children: (
                        <>
                          <Select
                            aria-label="选择草稿"
                            style={{ width: '100%', marginBottom: 8 }}
                            value={draftIndex}
                            onChange={handleDraftChange}
                            options={selectedArticle.pr_drafts?.map((draft, index) => ({
                              label: `${draft.template}-${draft.index} (${draft.perspective})`,
                              value: index,
                            }))}
                            size="small"
                          />
                          <Space>
                            <Tag color="blue">{currentDraft.template}</Tag>
                            <Tag>{currentDraft.perspective}</Tag>
                          </Space>
                        </>
                      ),
                    },
                  ]
                : []),
            ]}
          />

          {selectedArticle && currentDraft && (
            <>
              {/* 草稿预览 */}
              <Card
                className={styles.card}
                size="small"
                title={<Text className={styles.cardHeader}>{previewTitle}</Text>}
                extra={
                  <Button
                    type="text"
                    size="small"
                    aria-label="全屏预览"
                    icon={<ExpandAltOutlined />}
                    onClick={() => setDraftFullscreen(true)}
                    disabled={!previewContent}
                  >
                    全屏预览
                  </Button>
                }
              >
                <div className={styles.previewArea}>
                  {previewContent ? (
                    <div className={styles.markdownContent}>
                      <DraftBlockView
                        content={previewContent}
                        selectedBlockIndex={selectedBlock?.index ?? null}
                        onSelectBlock={handleSelectBlock}
                      />
                    </div>
                  ) : (
                    <Empty description="草稿内容不可用" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                  )}
                </div>
                {(viewingRevision || revisionResult) && (
                  <Space style={{ marginTop: 12 }}>
                    <Button size="small" icon={<CopyOutlined />} onClick={handleCopy}>
                      复制
                    </Button>
                    <Button size="small" icon={<DownloadOutlined />} onClick={handleDownload}>
                      下载
                    </Button>
                    {viewingRevision && !viewingRevision.applied && (
                      <Button
                        size="small"
                        type="primary"
                        onClick={() => handleApplyRevision(viewingRevision)}
                        loading={applying}
                      >
                        应用为当前稿
                      </Button>
                    )}
                  </Space>
                )}
                {feedbackRevisionId && (
                  <DraftFeedback
                    articleUrlHash={selectedArticle.url_hash}
                    draftIndex={draftIndex}
                    template={currentDraft.template}
                    perspective={currentDraft.perspective}
                    revisionId={feedbackRevisionId}
                    compact
                    onSubmitted={() => {
                      void refreshArticle();
                    }}
                  />
                )}
              </Card>

              <DraftReviewPanel
                review={currentDraft.review}
                contentMd={currentDraft.content_md}
                reviewing={reviewing}
                onReview={handleReviewDraft}
                compact
              />

              {/* 修订记录 */}
              <Card className={styles.card} size="small">
                <Card.Meta
                  title={
                    <Text strong className={styles.cardHeader}>
                      修订记录 ({revisions.length})
                    </Text>
                  }
                />
                <div className={styles.revisionsArea}>
                  <RevisionList
                    revisions={revisions}
                    selectedRevisionId={viewingRevision?.revision_id || null}
                    onSelect={handleSelectRevision}
                    onApply={handleApplyRevision}
                    applying={applying}
                  />
                </div>
              </Card>
            </>
          )}

          {!selectedArticle && !articlesLoading && (
            <div className={styles.emptyState}>
              <div className={styles.emptyIcon}>📝</div>
              <div className={styles.emptyText}>请选择文章开始对话改稿</div>
            </div>
          )}
        </Sider>

        <div
          className={styles.resizeHandle}
          role="separator"
          aria-label="调整文章选择栏宽度"
          aria-orientation="vertical"
          aria-valuemin={MIN_SIDER_WIDTH}
          aria-valuemax={getMaximumSiderWidth(getLayoutWidth())}
          aria-valuenow={siderWidth}
          tabIndex={0}
          title="拖动调整宽度，双击恢复默认"
          onPointerDown={handleResizeStart}
          onPointerMove={handleResizeMove}
          onPointerUp={handleResizeEnd}
          onPointerCancel={handleResizeEnd}
          onKeyDown={handleResizeKeyDown}
          onDoubleClick={resetSiderWidth}
        />

        {/* ── 右栏：对话区 ── */}
        <Content className={styles.content}>
          {/* 模式切换 */}
          <div className={styles.modeSwitch}>
            <Radio.Group
              value={mode}
              onChange={(e) => setMode(e.target.value as ChatMode)}
              options={modeOptions}
              buttonStyle="solid"
              size="large"
            />
            {mode === '改稿' && (
              <Text type="secondary" style={{ marginLeft: 12 }}>
                {selectedArticle ? `将对草稿 ${draftIndex + 1} 进行改稿` : '请先选择文章和草稿'}
              </Text>
            )}
            {messages.length > 0 && selectedArticle && (
              <Button
                size="small"
                icon={<ClearOutlined />}
                onClick={handleClearHistory}
                style={{ marginLeft: 'auto' }}
              >
                清空对话
              </Button>
            )}
          </div>

          {/* 错误提示 */}
          {error && (
            <Alert
              message={error}
              type="error"
              closable
              onClose={() => setError(null)}
              className={styles.errorAlert}
            />
          )}

          {/* 消息列表 */}
          <div className={styles.messagesContainer} ref={messagesContainerRef}>
            <div className={styles.messagesList}>
              {historyLoading ? (
                <div className={styles.loadingIndicator}>
                  <Spin size="small" />
                  <span>加载对话历史...</span>
                </div>
              ) : messages.length === 0 ? (
                <div className={styles.emptyState}>
                  <div className={styles.emptyIcon}>💬</div>
                  <div className={styles.emptyText}>
                    {mode === '问答' ? '输入问题开始对话' : '输入修改意见生成修订稿'}
                  </div>
                </div>
              ) : (
                messages.map((msg, i) => (
                  // biome-ignore lint/suspicious/noArrayIndexKey: 消息没有稳定 ID，且列表只追加不重排。
                  <ChatBubble key={i} message={msg} index={i} />
                ))
              )}
              {sending &&
                !(
                  messages.length > 0 &&
                  messages[messages.length - 1]?.role === 'assistant' &&
                  messages[messages.length - 1]?.content
                ) && (
                  <div className={styles.loadingIndicator}>
                    <Spin size="small" />
                    <span>{mode === '问答' ? '思考中...' : '生成修订稿中...'}</span>
                  </div>
                )}
              <div ref={messagesEndRef} />
            </div>
          </div>

          {/* 输入区 */}
          <div className={styles.inputArea}>
            {selectedBlock && (
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  padding: '4px 12px',
                  background: '#e6f4ff',
                  border: '1px solid #91caff',
                  borderRadius: 6,
                  marginBottom: 8,
                  fontSize: 13,
                }}
              >
                <EditOutlined style={{ color: '#1677ff' }} />
                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  已选中第 {selectedBlock.index + 1} 段：
                  {selectedBlock.text.length > 50
                    ? selectedBlock.text.slice(0, 50) + '...'
                    : selectedBlock.text}
                </span>
                <Button
                  type="text"
                  size="small"
                  onClick={() => setSelectedBlock(null)}
                  style={{ color: '#999', padding: '0 4px' }}
                >
                  清除
                </Button>
              </div>
            )}
            <div className={styles.inputWrapper}>
              <TextArea
                className={styles.textArea}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder={
                  mode === '问答'
                    ? '输入问题...'
                    : '输入修改意见，如：标题更有冲击力，减少技术细节...'
                }
                autoSize={{ minRows: 2, maxRows: 4 }}
                onPressEnter={(e) => {
                  if (!e.shiftKey) {
                    e.preventDefault();
                    handleSend();
                  }
                }}
                disabled={sending}
                variant="borderless"
              />
              <Button
                className={styles.sendButton}
                type="primary"
                icon={<SendOutlined />}
                onClick={handleSend}
                loading={sending}
                disabled={!input.trim()}
                size="large"
              >
                发送
              </Button>
            </div>
            <div className={styles.hintText}>Shift + Enter 换行，Enter 发送</div>
          </div>
        </Content>
      </Layout>

      <Drawer
        title={`${selectedArticle?.title || '稿件'} - ${currentDraft?.template || '预览'}`}
        open={draftFullscreen}
        onClose={() => setDraftFullscreen(false)}
        width={typeof window !== 'undefined' && window.innerWidth < 768 ? '95%' : '60%'}
        destroyOnHidden
        footer={
          <Space wrap>
            <Button icon={<CopyOutlined />} onClick={handleCopy} disabled={!previewContent}>
              复制
            </Button>
            <Button icon={<DownloadOutlined />} onClick={handleDownload} disabled={!previewContent}>
              下载
            </Button>
            {viewingRevision && !viewingRevision.applied && (
              <Button
                type="primary"
                onClick={() => handleApplyRevision(viewingRevision)}
                loading={applying}
              >
                应用为当前稿
              </Button>
            )}
          </Space>
        }
      >
        {previewContent ? (
          <div className={styles.fullscreenMarkdown}>
            <DraftBlockView
              content={previewContent}
              selectedBlockIndex={selectedBlock?.index ?? null}
              onSelectBlock={handleSelectBlock}
            />
          </div>
        ) : (
          <Empty description="草稿内容不可用" />
        )}
      </Drawer>
    </>
  );
}
