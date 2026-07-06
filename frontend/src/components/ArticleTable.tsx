/**
 * Article table with pagination, sort, scores, and action buttons.
 */

import { useCallback, useState } from "react";
import type { ColumnsType, TablePaginationConfig } from "antd/es/table";
import type { SorterResult } from "antd/es/table/interface";
import { Button, message, Popconfirm, Popover, Progress, Space, Table, Tag, Typography } from "antd";
import {
  DeleteOutlined, EyeOutlined, FileSearchOutlined, FileTextOutlined,
  PlayCircleOutlined, RobotOutlined,
} from "@ant-design/icons";
import api from "../api/client";
import type { Article } from "../types";

interface ArticleTableProps {
  articles: Article[];
  total: number;
  loading: boolean;
  page: number;
  pageSize: number;
  onPageChange: (page: number, pageSize: number) => void;
  onSortChange: (field: string, order: "asc" | "desc" | undefined) => void;
  onViewReport: (article: Article) => void;
  onViewDetail: (article: Article) => void;
  onViewDrafts: (article: Article) => void;
  onRunV2Single: (article: Article) => void;
  onRefresh: () => void;  // called after fetch/summarize to reload data
}

export default function ArticleTable({
  articles, total, loading, page, pageSize,
  onPageChange, onSortChange, onViewReport, onViewDetail, onViewDrafts, onRunV2Single, onRefresh,
}: ArticleTableProps) {
  const [busy, setBusy] = useState<Set<string>>(new Set());

  const handleFetch = useCallback(async (hash: string) => {
    setBusy((prev) => new Set(prev).add(hash));
    try {
      const r = await api.fetchContent(hash);
      if (r.ok) { message.success("fetched"); onRefresh(); }
    } catch { message.error("fetch failed"); }
    setBusy((prev) => { const n = new Set(prev); n.delete(hash); return n; });
  }, [onRefresh]);

  const handleSummarize = useCallback(async (hash: string) => {
    setBusy((prev) => new Set(prev).add(hash));
    try {
      const r = await api.summarizeArticle(hash);
      if (r.ok) { message.success("summarized"); onRefresh(); }
    } catch { message.error("summary failed"); }
    setBusy((prev) => { const n = new Set(prev); n.delete(hash); return n; });
  }, [onRefresh]);

  const columns: ColumnsType<Article> = [
    { title: "来源", dataIndex: "source", key: "source", width: 120, ellipsis: true,
      render: (s: string, r: Article) => <Tag color={r.source_type === "wechat_mp" ? "green" : "blue"}>{s}</Tag> },
    { title: "时间", dataIndex: "published_at", key: "published_at", width: 120, sorter: true,
      render: (v: string) => v?.slice(0, 11) || "-" },
    { title: "题目", dataIndex: "title", key: "title", width: 250, ellipsis: true,
      render: (t: string, r: Article) => <a href={r.url} target="_blank" rel="noopener noreferrer">{t}</a> },
    { title: "分类", dataIndex: "category", key: "category", width: 100, sorter: true,
      render: (c: string) => c ? <Tag>{c}</Tag> : <Tag color="default">-</Tag> },
    { title: "V2分类", dataIndex: "category_v2", key: "category_v2", width: 130,
      render: (c: string, r: Article) => {
        const colorMap: Record<string, string> = {
          "爆点事件": "red",
          "法律法规/监管动态": "orange",
          "AI技术重大进展": "purple",
          "国内外竞品信息": "blue",
          "运营商/行业事件": "cyan",
          "学术/会展/高校": "green",
        };
        const color = c ? (colorMap[c] || "default") : "default";
        const icon = r.is_pr_eligible ? " 🔥" : "";
        return c ? <Tag color={color}>{c}{icon}</Tag> : <Tag color="default">-</Tag>;
      }},
    { title: "相关度", dataIndex: "ai_relevance_score", key: "ai_relevance_score", width: 90, sorter: true,
      render: (s: number) => <Progress percent={s} size="small" strokeColor={s >= 70 ? "#52c41a" : s >= 40 ? "#faad14" : "#ff4d4f"} format={() => s} /> },
    { title: "可报道", dataIndex: "reportability_score", key: "reportability_score", width: 90, sorter: true,
      render: (s: number) => <Progress percent={s} size="small" strokeColor={s >= 70 ? "#722ed1" : s >= 40 ? "#faad14" : "#ff4d4f"} format={() => s} /> },
    { title: "综合分", dataIndex: "total_score", key: "total_score", width: 75, sorter: true,
      render: (s: number) => <Tag color={s >= 140 ? "red" : s >= 100 ? "orange" : "default"}>{s}</Tag> },
    { title: "产品相关", dataIndex: "product_relevance", key: "product_relevance", width: 85, sorter: true,
      render: (s: number) => s ? <Progress percent={s} size="small" strokeColor={s >= 70 ? "#1890ff" : s >= 40 ? "#faad14" : "#ff4d4f"} format={() => s} /> : null },
    { title: "事件影响", dataIndex: "event_impact", key: "event_impact", width: 85, sorter: true,
      render: (s: number) => s ? <Progress percent={s} size="small" strokeColor={s >= 70 ? "#722ed1" : s >= 40 ? "#faad14" : "#ff4d4f"} format={() => s} /> : null },
    { title: "V2综合", dataIndex: "pr_total_score", key: "pr_total_score", width: 70, sorter: true,
      render: (s: number) => s ? <Tag color={s >= 80 ? "red" : "orange"}>{s}</Tag> : null },
    { title: "摘要", dataIndex: "summary_cn", key: "summary_cn", width: 220,
      render: (v: string) => v ? (
        <Space size={0}>
          <Typography.Text ellipsis style={{ maxWidth: 150 }}>{v}</Typography.Text>
          <Popover content={<div style={{ width: 260, maxHeight: 180, overflow: "auto", lineHeight: 1.6 }}>{v}</div>} trigger="click">
            <Button type="link" size="small">{">"}</Button>
          </Popover>
        </Space>
      ) : null },
    {
      title: "操作", key: "actions", width: 260, fixed: "right",
      render: (_: unknown, record: Article) => {
        const b = busy.has(record.url_hash);
        const hasContent = !!record.content_md;
        const hasSummary = !!record.summary_cn;
        return (
          <Space size="small">
            <Button type="link" size="small" icon={<PlayCircleOutlined />}
              onClick={() => onRunV2Single(record)}>V2</Button>
            {record.source_type === "wechat_mp" && !hasContent && (
              <Button type="link" size="small" icon={<FileTextOutlined />} loading={b}
                onClick={() => handleFetch(record.url_hash)}>原文</Button>
            )}
            {hasContent && !hasSummary && (
              <Button type="link" size="small" icon={<RobotOutlined />} loading={b}
                onClick={() => handleSummarize(record.url_hash)}>摘要</Button>
            )}
            {record.has_report && (
              <Button type="link" size="small" icon={<FileSearchOutlined />}
                onClick={() => onViewReport(record)}>报道</Button>
            )}
            {record.pr_drafts && record.pr_drafts.length > 0 && (
              <Button type="link" size="small" icon={<FileTextOutlined />}
                onClick={() => onViewDrafts(record)}>草稿</Button>
            )}
            <Button type="link" size="small" icon={<EyeOutlined />}
              onClick={() => onViewDetail(record)}>详情</Button>
            <Popconfirm title="确定删除？" onConfirm={async () => {
              try { await api.deleteArticle(record.url_hash); message.success("deleted"); onRefresh(); }
              catch { message.error("delete failed"); }
            }}>
              <Button type="link" size="small" danger icon={<DeleteOutlined />} />
            </Popconfirm>
          </Space>
        );
      },
    },
  ];

  const handleTableChange = (
    p: TablePaginationConfig, _f: any, s: SorterResult<Article> | SorterResult<Article>[],
  ) => {
    if (p.current && p.pageSize) onPageChange(p.current, p.pageSize);
    const sr = Array.isArray(s) ? s[0] : s;
    if (sr.field) onSortChange(sr.field as string, sr.order === "ascend" ? "asc" : sr.order === "descend" ? "desc" : undefined);
  };

  return (
    <Table<Article>
      columns={columns}
      dataSource={articles}
      rowKey="_id"
      loading={loading}
      onChange={handleTableChange}
      pagination={{ current: page, pageSize, total, showSizeChanger: true, pageSizeOptions: ["10", "20", "50"], showTotal: (t: number) => `total ${t}` }}
      scroll={{ x: 1300 }}
      size="small"
      locale={{ emptyText: "no data" }}
    />
  );
}
