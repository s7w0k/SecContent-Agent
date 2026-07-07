/**
 * 对话改稿工作台页面
 *
 * 左栏：文章选择 + 草稿选择 + 原稿/修订稿预览 + 修订记录列表
 * 右栏：消息列表 + 输入框 + 模式切换（问答/改稿）
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Layout,
  message,
  Radio,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import {
  CopyOutlined,
  DownloadOutlined,
  QuestionCircleOutlined,
  EditOutlined,
  SendOutlined,
} from "@ant-design/icons";
import ReactMarkdown from "react-markdown";
import api, { chatApi } from "../api/client";
import ChatBubble from "../components/ChatBubble";
import RevisionList from "../components/RevisionList";
import styles from "./ChatPage.module.css";
import type {
  Article,
  ChatMessage,
  DraftRevision,
  DraftReviseResponse,
} from "../types";

const { Sider, Content } = Layout;
const { Text } = Typography;
const { TextArea } = Input;

type ChatMode = "问答" | "改稿";

const modeOptions = [
  { value: "问答", label: "问答", icon: <QuestionCircleOutlined /> },
  { value: "改稿", label: "改稿", icon: <EditOutlined /> },
];

export default function ChatPage() {
  // ── 文章 & 草稿 ──────────────────────────────────────────
  const [articles, setArticles] = useState<Article[]>([]);
  const [articlesLoading, setArticlesLoading] = useState(true);
  const [selectedArticle, setSelectedArticle] = useState<Article | null>(null);
  const [draftIndex, setDraftIndex] = useState<number>(0);

  // ── 对话 ─────────────────────────────────────────────────
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [mode, setMode] = useState<ChatMode>("问答");
  const [sending, setSending] = useState(false);

  // ── 修订稿预览 ───────────────────────────────────────────
  const [revisionResult, setRevisionResult] = useState<DraftReviseResponse | null>(null);
  const [viewingRevision, setViewingRevision] = useState<DraftRevision | null>(null);

  // ── 应用修订 ─────────────────────────────────────────────
  const [applying, setApplying] = useState(false);

  // ── 错误 ─────────────────────────────────────────────────
  const [error, setError] = useState<string | null>(null);

  const messagesEndRef = useRef<HTMLDivElement>(null);

  // ── 加载有草稿的文章列表 ──────────────────────────────────
  const loadArticles = useCallback(async () => {
    setArticlesLoading(true);
    try {
      const resp = await api.getArticles({ page: 1, page_size: 100 });
      const withDrafts = resp.items.filter((a) => a.pr_drafts && a.pr_drafts.length > 0);
      setArticles(withDrafts);
    } catch {
      setError("加载文章列表失败");
    } finally {
      setArticlesLoading(false);
    }
  }, []);

  useEffect(() => {
    loadArticles();
  }, [loadArticles]);

  // ── 消息列表滚动到底部 ────────────────────────────────────
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // ── 选择文章 ─────────────────────────────────────────────
  const handleArticleChange = (urlHash: string) => {
    const article = articles.find((a) => a.url_hash === urlHash) || null;
    setSelectedArticle(article);
    setDraftIndex(0);
    setMessages([]);
    setRevisionResult(null);
    setViewingRevision(null);
    setError(null);
  };

  // ── 选择草稿 ─────────────────────────────────────────────
  const handleDraftChange = (index: number) => {
    setDraftIndex(index);
    setRevisionResult(null);
    setViewingRevision(null);
  };

  // ── 发送消息 ─────────────────────────────────────────────
  const handleSend = async () => {
    const text = input.trim();
    if (!text) return;

    if (mode === "改稿" && !selectedArticle) {
      message.warning("改稿模式需要先选择文章和草稿");
      return;
    }

    const userMsg: ChatMessage = { role: "user", content: text };
    const newMessages = [...messages, userMsg];
    setMessages(newMessages);
    setInput("");
    setSending(true);
    setError(null);

    try {
      if (mode === "问答") {
        const resp = await chatApi.ask({
          message: text,
          article_url_hash: selectedArticle?.url_hash,
          draft_index: selectedArticle ? draftIndex : undefined,
          history: messages.length > 0 ? messages : undefined,
        });
        setMessages([...newMessages, { role: "assistant", content: resp.answer }]);
      } else {
        const resp = await chatApi.reviseDraft(
          selectedArticle!.url_hash,
          draftIndex,
          { instruction: text, save: true },
        );
        setRevisionResult(resp);
        setViewingRevision(null);
        setMessages([
          ...newMessages,
          {
            role: "assistant",
            content: `已生成修订稿。\n\n修改摘要：\n${resp.change_summary.map((s) => `- ${s}`).join("\n")}`,
          },
        ]);
        message.success("修订稿已生成并保存");
        await refreshArticle();
      }
    } catch (err: any) {
      const errMsg = err?.response?.data?.detail || err?.message || "请求失败";
      setError(errMsg);
      setMessages([
        ...newMessages,
        { role: "assistant", content: `错误：${errMsg}` },
      ]);
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
      setArticles((prev) =>
        prev.map((a) => (a.url_hash === updated.url_hash ? updated : a)),
      );
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
      await chatApi.applyRevision(
        selectedArticle.url_hash,
        draftIndex,
        rev.revision_id,
      );
      message.success("修订已应用为当前稿");
      await refreshArticle();
      setViewingRevision(null);
      setRevisionResult(null);
    } catch (err: any) {
      const errMsg = err?.response?.data?.detail || err?.message || "应用失败";
      message.error(errMsg);
    } finally {
      setApplying(false);
    }
  };

  // ── 复制修订稿 ────────────────────────────────────────────
  const handleCopy = () => {
    const content = viewingRevision?.content_md || revisionResult?.revised_content_md;
    if (!content) return;
    navigator.clipboard
      .writeText(content)
      .then(() => message.success("已复制"))
      .catch(() => message.error("复制失败"));
  };

  // ── 下载修订稿 ────────────────────────────────────────────
  const handleDownload = () => {
    const content = viewingRevision?.content_md || revisionResult?.revised_content_md;
    const revId = viewingRevision?.revision_id || revisionResult?.revision_id || "revision";
    if (!content) return;
    const blob = new Blob([content], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `PR-revision-${revId.slice(0, 8)}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  // ── 当前草稿 ─────────────────────────────────────────────
  const currentDraft = selectedArticle?.pr_drafts?.[draftIndex];
  const revisions: DraftRevision[] = currentDraft?.revisions || [];

  // ── 预览内容 ─────────────────────────────────────────────
  const previewContent = viewingRevision?.content_md
    || revisionResult?.revised_content_md
    || currentDraft?.content_md
    || "";
  const previewTitle = viewingRevision
    ? "修订稿预览"
    : revisionResult
      ? "修订稿预览"
      : "原稿预览";

  return (
    <Layout className={styles.layout}>
      {/* ── 左栏：选择 + 预览 + 修订记录 ── */}
      <Sider width={420} className={styles.sider}>
        {/* 文章选择 */}
        <Card className={styles.card} size="small">
          <Card.Meta title={<Text strong className={styles.cardHeader}>文章选择</Text>} />
          {articlesLoading ? (
            <div className={styles.loadingIndicator}>
              <Spin size="small" />
              <span>加载中...</span>
            </div>
          ) : (
            <Select
              showSearch
              placeholder="选择有草稿的文章"
              style={{ width: "100%" }}
              value={selectedArticle?.url_hash}
              onChange={handleArticleChange}
              options={articles.map((a) => ({
                label: a.title?.slice(0, 50),
                value: a.url_hash,
              }))}
              optionFilterProp="label"
              size="small"
            />
          )}
        </Card>

        {selectedArticle && currentDraft && (
          <>
            {/* 草稿信息 */}
            <Card className={styles.card} size="small">
              <Card.Meta title={<Text strong className={styles.cardHeader}>草稿选择</Text>} />
              <Select
                style={{ width: "100%", marginBottom: 8 }}
                value={draftIndex}
                onChange={handleDraftChange}
                options={selectedArticle.pr_drafts?.map((d, i) => ({
                  label: `${d.template}-${d.index} (${d.perspective})`,
                  value: i,
                }))}
                size="small"
              />
              <Space>
                <Tag color="blue">{currentDraft.template}</Tag>
                <Tag>{currentDraft.perspective}</Tag>
              </Space>
            </Card>

            {/* 草稿预览 */}
            <Card className={styles.card} size="small">
              <Card.Meta title={<Text strong className={styles.cardHeader}>{previewTitle}</Text>} />
              <div className={styles.previewArea}>
                {previewContent ? (
                  <div className={styles.markdownContent}>
                    <ReactMarkdown>{previewContent}</ReactMarkdown>
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
                  <Button
                    size="small"
                    icon={<DownloadOutlined />}
                    onClick={handleDownload}
                  >
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
            </Card>

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
          {mode === "改稿" && (
            <Text type="secondary" style={{ marginLeft: 12 }}>
              {selectedArticle
                ? `将对草稿 ${draftIndex + 1} 进行改稿`
                : "请先选择文章和草稿"}
            </Text>
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
        <div className={styles.messagesContainer}>
          <div className={styles.messagesList}>
            {messages.length === 0 ? (
              <div className={styles.emptyState}>
                <div className={styles.emptyIcon}>💬</div>
                <div className={styles.emptyText}>
                  {mode === "问答"
                    ? "输入问题开始对话"
                    : "输入修改意见生成修订稿"}
                </div>
              </div>
            ) : (
              messages.map((msg, i) => (
                <ChatBubble key={i} message={msg} index={i} />
              ))
            )}
            {sending && (
              <div className={styles.loadingIndicator}>
                <Spin size="small" />
                <span>{mode === "问答" ? "思考中..." : "生成修订稿中..."}</span>
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>
        </div>

        {/* 输入区 */}
        <div className={styles.inputArea}>
          <div className={styles.inputWrapper}>
            <TextArea
              className={styles.textArea}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={
                mode === "问答"
                  ? "输入问题..."
                  : "输入修改意见，如：标题更有冲击力，减少技术细节..."
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
          <div className={styles.hintText}>
            Shift + Enter 换行，Enter 发送
          </div>
        </div>
      </Content>
    </Layout>
  );
}
