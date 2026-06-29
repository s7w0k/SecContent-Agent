/**
 * 文章表格组件
 *
 * 分页展示文章列表，支持排序、分数可视化、操作按钮。
 *
 * Props:
 *   articles: Article[]          — 文章列表
 *   total: number                — 总条数（服务端分页用）
 *   loading: boolean             — 加载态
 *   pagination: { page, pageSize }
 *   onPageChange: (page, pageSize) => void
 *   onSortChange: (field, order) => void
 *   onViewReport: (article) => void
 *   onViewDetail: (article) => void
 */

import type { ColumnsType, TablePaginationConfig } from "antd/es/table";
import type { SorterResult } from "antd/es/table/interface";
import { Button, Progress, Space, Table, Tag } from "antd";
import {
  EyeOutlined,
  FileSearchOutlined,
} from "@ant-design/icons";
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
}

export default function ArticleTable({
  articles,
  total,
  loading,
  page,
  pageSize,
  onPageChange,
  onSortChange,
  onViewReport,
  onViewDetail,
}: ArticleTableProps) {
  const columns: ColumnsType<Article> = [
    {
      title: "来源",
      dataIndex: "source",
      key: "source",
      width: 140,
      ellipsis: true,
      render: (source: string) => (
        <Tag color="blue">{source}</Tag>
      ),
    },
    {
      title: "入库时间",
      dataIndex: "added_at",
      key: "added_at",
      width: 110,
      sorter: true,
      render: (val: string) => val?.slice(0, 10) || "-",
    },
    {
      title: "题目",
      dataIndex: "title",
      key: "title",
      ellipsis: true,
      render: (title: string, record: Article) => (
        <a href={record.url} target="_blank" rel="noopener noreferrer">
          {title}
        </a>
      ),
    },
    {
      title: "分类",
      dataIndex: "category",
      key: "category",
      width: 130,
      sorter: true,
      render: (cat: string) =>
        cat ? <Tag>{cat}</Tag> : <Tag color="default">未分类</Tag>,
    },
    {
      title: "AI相关度",
      dataIndex: "ai_relevance_score",
      key: "ai_relevance_score",
      width: 110,
      sorter: true,
      render: (score: number) => (
        <Progress
          percent={score}
          size="small"
          strokeColor={score >= 70 ? "#52c41a" : score >= 40 ? "#faad14" : "#ff4d4f"}
          format={() => score}
        />
      ),
    },
    {
      title: "可报道性",
      dataIndex: "reportability_score",
      key: "reportability_score",
      width: 110,
      sorter: true,
      render: (score: number) => (
        <Progress
          percent={score}
          size="small"
          strokeColor={score >= 70 ? "#722ed1" : score >= 40 ? "#faad14" : "#ff4d4f"}
          format={() => score}
        />
      ),
    },
    {
      title: "综合分",
      dataIndex: "total_score",
      key: "total_score",
      width: 85,
      sorter: true,
      render: (score: number) => (
        <Tag color={score >= 140 ? "red" : score >= 100 ? "orange" : "default"}>
          {score}
        </Tag>
      ),
    },
    {
      title: "操作",
      key: "actions",
      width: 150,
      render: (_: unknown, record: Article) => (
        <Space size="small">
          {record.has_report && (
            <Button
              type="link"
              size="small"
              icon={<FileSearchOutlined />}
              onClick={() => onViewReport(record)}
            >
              报道
            </Button>
          )}
          <Button
            type="link"
            size="small"
            icon={<EyeOutlined />}
            onClick={() => onViewDetail(record)}
          >
            详情
          </Button>
        </Space>
      ),
    },
  ];

  const handleTableChange = (
    pagination: TablePaginationConfig,
    _filters: Record<string, unknown>,
    sorter: SorterResult<Article> | SorterResult<Article>[],
  ) => {
    if (pagination.current && pagination.pageSize) {
      onPageChange(pagination.current, pagination.pageSize);
    }

    const s = Array.isArray(sorter) ? sorter[0] : sorter;
    if (s.field) {
      const order = s.order === "ascend" ? "asc" : s.order === "descend" ? "desc" : undefined;
      onSortChange(s.field as string, order);
    }
  };

  return (
    <Table<Article>
      columns={columns}
      dataSource={articles}
      rowKey="_id"
      loading={loading}
      onChange={handleTableChange}
      pagination={{
        current: page,
        pageSize: pageSize,
        total: total,
        showSizeChanger: true,
        pageSizeOptions: ["10", "20", "50"],
        showTotal: (t: number) => `共 ${t} 条`,
      }}
      scroll={{ x: 1000 }}
      locale={{ emptyText: "暂无文章数据" }}
    />
  );
}
