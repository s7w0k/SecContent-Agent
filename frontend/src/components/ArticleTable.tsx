/**
 * Article table with pagination, sort, scores, and action buttons.
 */

import {
  DeleteOutlined,
  ExperimentOutlined,
  EyeOutlined,
  FileSearchOutlined,
  FileTextOutlined,
  PlayCircleOutlined,
  RobotOutlined,
} from '@ant-design/icons';
import {
  Button,
  Popconfirm,
  Popover,
  Progress,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType, TablePaginationConfig } from 'antd/es/table';
import type { FilterValue, SorterResult } from 'antd/es/table/interface';
import { useCallback, useState } from 'react';
import type { Key } from 'react';
import api from '../api/client';
import type { Article } from '../types';

interface ArticleTableProps {
  articles: Article[];
  total: number;
  loading: boolean;
  page: number;
  pageSize: number;
  onPageChange: (page: number, pageSize: number) => void;
  onSortChange: (field: string, order: 'asc' | 'desc' | undefined) => void;
  onViewReport: (article: Article) => void;
  onViewDetail: (article: Article) => void;
  onViewDrafts: (article: Article) => void;
  onRunV2Single: (article: Article) => void;
  onScoreV2Single: (article: Article) => void;
  onRefresh: () => void; // called after fetch/summarize to reload data
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
  onViewDrafts,
  onRunV2Single,
  onScoreV2Single,
  onRefresh,
}: ArticleTableProps) {
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const [selectedRowKeys, setSelectedRowKeys] = useState<Key[]>([]);
  const [batchDeleting, setBatchDeleting] = useState(false);
  const [deletingIrrelevant, setDeletingIrrelevant] = useState(false);

  const handleFetch = useCallback(
    async (hash: string) => {
      setBusy((prev) => new Set(prev).add(hash));
      try {
        const r = await api.fetchContent(hash);
        if (r.ok) {
          message.success('fetched');
          onRefresh();
        }
      } catch {
        message.error('fetch failed');
      }
      setBusy((prev) => {
        const n = new Set(prev);
        n.delete(hash);
        return n;
      });
    },
    [onRefresh],
  );

  const handleSummarize = useCallback(
    async (hash: string) => {
      setBusy((prev) => new Set(prev).add(hash));
      try {
        const r = await api.summarizeArticle(hash);
        if (r.ok) {
          message.success('summarized');
          onRefresh();
        }
      } catch {
        message.error('summary failed');
      }
      setBusy((prev) => {
        const n = new Set(prev);
        n.delete(hash);
        return n;
      });
    },
    [onRefresh],
  );

  const handleBatchDelete = useCallback(async () => {
    const hashes = articles.filter((a) => selectedRowKeys.includes(a._id)).map((a) => a.url_hash);
    if (!hashes.length) return;
    setBatchDeleting(true);
    try {
      const r = await api.batchDeleteArticles(hashes);
      message.success(`已删除 ${r.deleted} 篇`);
      setSelectedRowKeys([]);
      onRefresh();
    } catch {
      message.error('批量删除失败');
    }
    setBatchDeleting(false);
  }, [articles, selectedRowKeys, onRefresh]);

  const handleDeleteIrrelevant = useCallback(async () => {
    setDeletingIrrelevant(true);
    try {
      const r = await api.deleteIrrelevantArticles();
      message.success(`已删除 ${r.deleted} 篇不相关文章`);
      onRefresh();
    } catch {
      message.error('删除不相关文章失败');
    }
    setDeletingIrrelevant(false);
  }, [onRefresh]);

  const columns: ColumnsType<Article> = [
    {
      title: '来源',
      dataIndex: 'source',
      key: 'source',
      width: 120,
      ellipsis: true,
      render: (s: string, r: Article) => {
        const sourceStyle = {
          overseas_news: { color: 'blue', label: s },
          wechat_mp: { color: 'green', label: s },
          paper: { color: 'default', label: s },
          user_upload: { color: 'purple', label: '用户上传' },
        }[r.source_type];
        return <Tag color={sourceStyle.color}>{sourceStyle.label}</Tag>;
      },
    },
    {
      title: '时间',
      dataIndex: 'published_at',
      key: 'published_at',
      width: 120,
      sorter: true,
      render: (v: string) => v?.slice(0, 11) || '-',
    },
    {
      title: '题目',
      dataIndex: 'title',
      key: 'title',
      width: 250,
      ellipsis: true,
      render: (t: string, r: Article) => (
        <a href={r.url} target="_blank" rel="noopener noreferrer">
          {t}
        </a>
      ),
    },
    {
      title: 'V2分类',
      dataIndex: 'category_v2',
      key: 'category_v2',
      width: 130,
      render: (c: string, r: Article) => {
        const colorMap: Record<string, string> = {
          爆点事件: 'red',
          '法律法规/监管动态': 'orange',
          AI技术重大进展: 'purple',
          国内外竞品信息: 'blue',
          '运营商/行业事件': 'cyan',
          '学术/会展/高校': 'green',
        };
        const color = c ? colorMap[c] || 'default' : 'default';
        const icon = r.is_pr_eligible ? ' 🔥' : '';
        const categoryTag = c ? (
          <Tag color={color}>
            {c}
            {icon}
          </Tag>
        ) : (
          <Tag color="default">-</Tag>
        );
        if (c !== '不相关') return categoryTag;
        return (
          <Popover
            title="AI/智能体安全相关性判断"
            content={
              <Space direction="vertical" size={2} style={{ maxWidth: 320 }}>
                <Typography.Text>
                  {r.ai_agent_security_relevance_reason || '原文未发现直接相关内容'}
                </Typography.Text>
                <Typography.Text type="secondary">
                  置信度：{r.ai_agent_security_relevance_confidence || 0}%
                </Typography.Text>
              </Space>
            }
          >
            {categoryTag}
          </Popover>
        );
      },
    },
    {
      title: '产品相关',
      dataIndex: 'product_relevance',
      key: 'product_relevance',
      width: 85,
      sorter: true,
      render: (s: number) =>
        s ? (
          <Progress
            percent={s}
            size="small"
            strokeColor={s >= 70 ? '#1890ff' : s >= 40 ? '#faad14' : '#ff4d4f'}
            format={() => s}
          />
        ) : null,
    },
    {
      title: '事件影响',
      dataIndex: 'event_impact',
      key: 'event_impact',
      width: 85,
      sorter: true,
      render: (s: number) =>
        s ? (
          <Progress
            percent={s}
            size="small"
            strokeColor={s >= 70 ? '#722ed1' : s >= 40 ? '#faad14' : '#ff4d4f'}
            format={() => s}
          />
        ) : null,
    },
    {
      title: 'V2综合',
      dataIndex: 'pr_total_score',
      key: 'pr_total_score',
      width: 70,
      sorter: true,
      render: (s: number) => (s ? <Tag color={s >= 80 ? 'red' : 'orange'}>{s}</Tag> : null),
    },
    {
      title: '摘要',
      dataIndex: 'summary_cn',
      key: 'summary_cn',
      width: 220,
      render: (v: string) =>
        v ? (
          <Space size={0}>
            <Typography.Text ellipsis style={{ maxWidth: 150 }}>
              {v}
            </Typography.Text>
            <Popover
              content={
                <div style={{ width: 260, maxHeight: 180, overflow: 'auto', lineHeight: 1.6 }}>
                  {v}
                </div>
              }
              trigger="click"
            >
              <Button type="link" size="small">
                {'>'}
              </Button>
            </Popover>
          </Space>
        ) : null,
    },
    {
      title: '操作',
      key: 'actions',
      width: 260,
      fixed: 'right',
      render: (_: unknown, record: Article) => {
        const b = busy.has(record.url_hash);
        const hasContent = !!record.content_md;
        const hasSummary = !!record.summary_cn;
        return (
          <Space size="small">
            <Button
              type="link"
              size="small"
              icon={<PlayCircleOutlined />}
              onClick={() => onRunV2Single(record)}
            >
              {record.pr_drafts?.length ? '重新生成' : '生成草稿'}
            </Button>
            <Button
              type="link"
              size="small"
              icon={<ExperimentOutlined />}
              onClick={() => onScoreV2Single(record)}
            >
              打分
            </Button>
            {record.source_type === 'wechat_mp' && !hasContent && (
              <Button
                type="link"
                size="small"
                icon={<FileTextOutlined />}
                loading={b}
                onClick={() => handleFetch(record.url_hash)}
              >
                原文
              </Button>
            )}
            {record.source_type === 'overseas_news' && !hasContent && (
              <Button
                type="link"
                size="small"
                icon={<FileTextOutlined />}
                loading={b}
                onClick={() => handleFetch(record.url_hash)}
              >
                抓取原文
              </Button>
            )}
            {hasContent && !hasSummary && (
              <Button
                type="link"
                size="small"
                icon={<RobotOutlined />}
                loading={b}
                onClick={() => handleSummarize(record.url_hash)}
              >
                摘要
              </Button>
            )}
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
            {record.pr_drafts && record.pr_drafts.length > 0 && (
              <Button
                type="link"
                size="small"
                icon={<FileTextOutlined />}
                onClick={() => onViewDrafts(record)}
              >
                草稿
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
            <Popconfirm
              title="确定删除？"
              onConfirm={async () => {
                try {
                  await api.deleteArticle(record.url_hash);
                  message.success('deleted');
                  onRefresh();
                } catch {
                  message.error('delete failed');
                }
              }}
            >
              <Button type="link" size="small" danger icon={<DeleteOutlined />} />
            </Popconfirm>
          </Space>
        );
      },
    },
  ];

  const handleTableChange = (
    p: TablePaginationConfig,
    _f: Record<string, FilterValue | null>,
    s: SorterResult<Article> | SorterResult<Article>[],
  ) => {
    if (p.current && p.pageSize) onPageChange(p.current, p.pageSize);
    const sr = Array.isArray(s) ? s[0] : s;
    if (sr.field)
      onSortChange(
        sr.field as string,
        sr.order === 'ascend' ? 'asc' : sr.order === 'descend' ? 'desc' : undefined,
      );
  };

  return (
    <>
      <div style={{ marginBottom: 12 }}>
        <Popconfirm
          title="确定删除所有分类为「不相关」的文章？"
          onConfirm={handleDeleteIrrelevant}
          disabled={deletingIrrelevant}
        >
          <Button danger size="small" icon={<DeleteOutlined />} loading={deletingIrrelevant}>
            删除不相关文章
          </Button>
        </Popconfirm>
      </div>
      {selectedRowKeys.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <Space>
            <span>已选择 {selectedRowKeys.length} 项</span>
            <Popconfirm
              title={`确定删除选中的 ${selectedRowKeys.length} 篇文章？`}
              onConfirm={handleBatchDelete}
              disabled={batchDeleting}
            >
              <Button danger size="small" icon={<DeleteOutlined />} loading={batchDeleting}>
                批量删除
              </Button>
            </Popconfirm>
            <Button size="small" onClick={() => setSelectedRowKeys([])}>
              取消选择
            </Button>
          </Space>
        </div>
      )}
      <Table<Article>
        columns={columns}
        dataSource={articles}
        rowKey="_id"
        loading={loading}
        onChange={handleTableChange}
        rowSelection={{
          selectedRowKeys,
          onChange: (keys) => setSelectedRowKeys(keys),
        }}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          pageSizeOptions: ['10', '20', '50'],
          showTotal: (t: number) => `total ${t}`,
        }}
        scroll={{ x: 1300 }}
        size="small"
        locale={{ emptyText: 'no data' }}
      />
    </>
  );
}
