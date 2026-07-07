/**
 * 对话改稿工作台页面
 *
 * 左栏：文章选择 + 草稿选择 + 原稿/修订稿预览
 * 右栏：消息列表 + 输入框 + 模式切换（问答/改稿）
 *
 * 问答模式：调用 /api/chat/ask
 * 改稿模式：调用 /api/articles/{hash}/drafts/{index}/revise
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Empty,
  Input,
  Layout,
  message,
  Segmented,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import { CopyOutlined, DownloadOutlined, SendOutlined } from "@ant-design/icons";
import ReactMarkdown from "react-markdown";
import api, { chatApi } from "../api/client";
import type {
  Article,
  ChatMessage,
  DraftReviseResponse,
} from "../types";

const { Sider, Content } = Layout;
const { Text } = Typography;

type ChatMode = "问答" | "改稿";

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

  // ── 修订稿 ───────────────────────────────────────────────
  const [revisionResult, setRevisionResult] = useState<DraftReviseResponse | null>(null);

  // ── 错误 ─────────────────────────────────────────────────
  const [error, setError] = useState<string | null>(null);

  const messagesEndRef = useRef<HTMLDivElement>(null);

  // ── 加载有草稿的文章列表 ──────────────────────────────────
  const loadArticles = useCallback(async () => {
    setArticlesLoading(true);
    try {
      const resp = await api.getArticles({ page: 1, page_size: 100 });
      // 只保留有 pr_drafts 的文章
      const withDrafts = resp.items.filter((a) => a.pr_drafts && a.pr_drafts.length > 0);
      setArticles(withDrafts);
    } catch (err) {
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
    setError(null);
  };

  // ── 选择草稿 ─────────────────────────────────────────────
  const handleDraftChange = (index: number) => {
    setDraftIndex(index);
    setRevisionResult(null);
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
        // 改稿模式
        const resp = await chatApi.reviseDraft(
          selectedArticle!.url_hash,
          draftIndex,
          { instruction: text, save: true },
        );
        setRevisionResult(resp);
        setMessages([
          ...newMessages,
          {
            role: "assistant",
            content: `已生成修订稿。\n\n修改摘要：\n${resp.change_summary.map((s) => `- ${s}`).join("\n")}`,
          },
        ]);
        message.success("修订稿已生成并保存");
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

  // ── 复制修订稿 ────────────────────────────────────────────
  const handleCopy = () => {
    if (!revisionResult?.revised_content_md) return;
    navigator.clipboard
      .writeText(revisionResult.revised_content_md)
      .then(() => message.success("已复制"))
      .catch(() => message.error("复制失败"));
  };

  // ── 下载修订稿 ────────────────────────────────────────────
  const handleDownload = () => {
    if (!revisionResult?.revised_content_md) return;
    const blob = new Blob([revisionResult.revised_content_md], {
      type: "text/markdown",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `PR-revision-${revisionResult.revision_id.slice(0, 8)}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  // ── 当前草稿 ─────────────────────────────────────────────
  const currentDraft = selectedArticle?.pr_drafts?.[draftIndex];

  return (
    <Layout style={{ minHeight: "calc(100vh - 64px)" }}>
      {/* ── 左栏：选择 + 预览 ── */}
      <Sider
        width={420}
        style={{
          background: "#fff",
          padding: "16px",
          overflow: "auto",
          borderRight: "1px solid #f0f0f0",
        }}
      >
        <Text strong style={{ display: "block", marginBottom: 8 }}>
          文章选择
        </Text>
        {articlesLoading ? (
          <Spin size="small" />
        ) : (
          <Select
            showSearch
            placeholder="选择有草稿的文章"
            style={{ width: "100%", marginBottom: 12 }}
            value={selectedArticle?.url_hash}
            onChange={handleArticleChange}
            options={articles.map((a) => ({
              label: a.title?.slice(0, 50),
              value: a.url_hash,
            }))}
            optionFilterProp="label"
          />
        )}

        {selectedArticle && currentDraft && (
          <>
            <Text strong style={{ display: "block", marginBottom: 8 }}>
              草稿选择
            </Text>
            <Select
              style={{ width: "100%", marginBottom: 12 }}
              value={draftIndex}
              onChange={handleDraftChange}
              options={selectedArticle.pr_drafts?.map((d, i) => ({
                label: `${d.template}-${d.index} (${d.perspective})`,
                value: i,
              }))}
            />

            <Space style={{ marginBottom: 8 }}>
              <Tag color="blue">{currentDraft.template}</Tag>
              <Tag>{currentDraft.perspective}</Tag>
            </Space>

            <Text strong style={{ display: "block", marginBottom: 4 }}>
              {revisionResult ? "修订稿预览" : "原稿预览"}
            </Text>
            <div
              style={{
                maxHeight: "calc(100vh - 380px)",
                overflow: "auto",
                padding: "8px",
                background: "#fafafa",
                borderRadius: 6,
              }}
            >
              {revisionResult ? (
                <ReactMarkdown>{revisionResult.revised_content_md}</ReactMarkdown>
              ) : currentDraft.content_md ? (
                <ReactMarkdown>{currentDraft.content_md}</ReactMarkdown>
              ) : (
                <Empty description="草稿内容不可用" />
              )}
            </div>

            {revisionResult && (
              <Space style={{ marginTop: 8 }}>
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
              </Space>
            )}
          </>
        )}

        {!selectedArticle && !articlesLoading && (
          <Empty description="请选择文章" style={{ marginTop: 48 }} />
        )}
      </Sider>

      {/* ── 右栏：对话区 ── */}
      <Content style={{ padding: "16px", display: "flex", flexDirection: "column" }}>
        <Space style={{ marginBottom: 12 }}>
          <Segmented
            value={mode}
            onChange={(v) => setMode(v as ChatMode)}
            options={["问答", "改稿"]}
          />
          {mode === "改稿" && (
            <Text type="secondary">
              {selectedArticle
                ? `将对草稿 ${draftIndex + 1} 进行改稿`
                : "请先选择文章和草稿"}
            </Text>
          )}
        </Space>

        {error && (
          <Alert
            message={error}
            type="error"
            closable
            onClose={() => setError(null)}
            style={{ marginBottom: 12 }}
          />
        )}

        {/* 消息列表 */}
        <div
          style={{
            flex: 1,
            overflow: "auto",
            padding: "8px",
            background: "#fafafa",
            borderRadius: 6,
          }}
        >
          {messages.length === 0 ? (
            <Empty
              description={
                mode === "问答"
                  ? "输入问题开始对话"
                  : "输入修改意见生成修订稿"
              }
              style={{ marginTop: 48 }}
            />
          ) : (
            messages.map((msg, i) => (
              <div
                key={i}
                style={{
                  marginBottom: 12,
                  textAlign: msg.role === "user" ? "right" : "left",
                }}
              >
                <div
                  style={{
                    display: "inline-block",
                    maxWidth: "80%",
                    padding: "8px 12px",
                    borderRadius: 8,
                    background:
                      msg.role === "user" ? "#1677ff" : "#fff",
                    color: msg.role === "user" ? "#fff" : "#333",
                    border:
                      msg.role === "user" ? "none" : "1px solid #e8e8e8",
                    textAlign: "left",
                    whiteSpace: "pre-wrap",
                  }}
                >
                  {msg.content}
                </div>
              </div>
            ))
          )}
          {sending && (
            <div style={{ textAlign: "left", marginBottom: 12 }}>
              <Spin size="small" />
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        {/* 输入区 */}
        <div style={{ marginTop: 12 }}>
          <Input.TextArea
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
          />
          <div style={{ marginTop: 8, textAlign: "right" }}>
            <Button
              type="primary"
              icon={<SendOutlined />}
              onClick={handleSend}
              loading={sending}
              disabled={!input.trim()}
            >
              发送
            </Button>
          </div>
        </div>
      </Content>
    </Layout>
  );
}
