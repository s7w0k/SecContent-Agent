/**
 * PR 报道查看器
 *
 * Modal 弹窗展示结构化 PR 报道，支持 Markdown 渲染、源文章对照、复制和下载。
 * 打开时自动从 API 加载报道详情。
 *
 * Props:
 *   reportId: string | null  — 报道 ID（null 时不打开）
 *   article: Article | null   — 关联的源文章（展示打分信息）
 *   onClose: () => void
 */

import { useCallback, useEffect, useState } from "react";
import {
  Button,
  Descriptions,
  Divider,
  message,
  Modal,
  Skeleton,
  Space,
  Tag,
  Typography,
} from "antd";
import {
  CopyOutlined,
  DownloadOutlined,
  LinkOutlined,
} from "@ant-design/icons";
import ReactMarkdown from "react-markdown";
import api from "../api/client";
import type { Article, Report } from "../types";

const { Text, Paragraph } = Typography;

interface ReportViewerProps {
  reportId: string | null;
  article: Article | null;
  onClose: () => void;
}

export default function ReportViewer({
  reportId,
  article,
  onClose,
}: ReportViewerProps) {
  const [report, setReport] = useState<Report | null>(null);
  const [loading, setLoading] = useState(false);

  // 加载报道详情
  useEffect(() => {
    if (!reportId) {
      setReport(null);
      return;
    }
    setLoading(true);
    api
      .getReport(reportId)
      .then(setReport)
      .catch(() => message.error("加载报道失败"))
      .finally(() => setLoading(false));
  }, [reportId]);

  // ── 复制 ──────────────────────────────────────────────────

  const handleCopy = useCallback(() => {
    if (!report?.content_md) return;
    navigator.clipboard
      .writeText(report.content_md)
      .then(() => message.success("已复制到剪贴板"))
      .catch(() => message.error("复制失败"));
  }, [report]);

  // ── 下载 ──────────────────────────────────────────────────

  const handleDownload = useCallback(() => {
    if (!report?.content_md) return;
    const blob = new Blob([report.content_md], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${report.title?.slice(0, 30) || "report"}.md`;
    a.click();
    URL.revokeObjectURL(url);
    message.success("下载完成");
  }, [report]);

  if (!reportId) return null;

  return (
    <Modal
      title={
        <Space>
          <span>📄 PR 报道</span>
          {report?.title && (
            <Text type="secondary">— {report.title}</Text>
          )}
        </Space>
      }
      open={!!reportId}
      onCancel={onClose}
      width={900}
      footer={
        <Space>
          <Button icon={<CopyOutlined />} onClick={handleCopy}>
            复制全文
          </Button>
          <Button icon={<DownloadOutlined />} onClick={handleDownload}>
            下载 .md
          </Button>
          <Button onClick={onClose}>关闭</Button>
        </Space>
      }
    >
      {loading ? (
        <Skeleton active paragraph={{ rows: 8 }} />
      ) : report ? (
        <>
          {/* 源文章信息 */}
          {article && (
            <>
              <Descriptions size="small" column={2} bordered>
                <Descriptions.Item label="源文章">
                  <a
                    href={article.url}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    <LinkOutlined /> {article.title}
                  </a>
                </Descriptions.Item>
                <Descriptions.Item label="来源">
                  <Tag color="blue">{article.source}</Tag>
                </Descriptions.Item>
                <Descriptions.Item label="综合分">
                  <Tag color={article.total_score >= 140 ? "red" : "default"}>
                    {article.total_score}
                  </Tag>
                </Descriptions.Item>
                <Descriptions.Item label="报道模板">
                  <Tag>{report.template}</Tag>
                </Descriptions.Item>
              </Descriptions>
              <Divider />
            </>
          )}

          {/* Markdown 渲染 */}
          <div
            className="report-content"
            style={{
              maxHeight: "60vh",
              overflow: "auto",
              padding: "8px 0",
            }}
          >
            <ReactMarkdown>{report.content_md}</ReactMarkdown>
          </div>
        </>
      ) : (
        <Paragraph type="secondary">报道数据不可用</Paragraph>
      )}
    </Modal>
  );
}
