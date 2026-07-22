/**
 * 仪表盘主页面
 *
 * 组合 StatsCards + FilterBar + ArticleTable + PipelineControl + ReportViewer，
 * 管理全局状态（筛选/分页/排序/报道查看）。
 */

import { ImportOutlined, UploadOutlined } from '@ant-design/icons';
import { Button, Col, Drawer, Row, Space, Tag, Typography, message } from 'antd';
import { useCallback, useEffect, useState } from 'react';
import api from '../api/client';
import ArticleTable from '../components/ArticleTable';
import ArticleUpload from '../components/ArticleUpload';
import CategoryBreakdown from '../components/CategoryBreakdown';
import DraftViewer from '../components/DraftViewer';
import FilterBar from '../components/FilterBar';
import HotRankingPanel from '../components/HotRankingPanel';
import PipelineControl from '../components/PipelineControl';
import PipelineTaskProgress from '../components/PipelineTaskProgress';
import ReportViewer from '../components/ReportViewer';
import StatsCards from '../components/StatsCards';
import TodayStatsRow from '../components/TodayStatsRow';
import type { Article, ArticleQuery, FilterValues, StatsData } from '../types';

const { Title, Paragraph, Text } = Typography;

function getRequestErrorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } }).response;
    if (typeof response?.data?.detail === 'string') return response.data.detail;
  }
  return error instanceof Error ? error.message : '未知错误';
}

export default function Dashboard() {
  // ── 数据状态 ──────────────────────────────────────────────
  const [stats, setStats] = useState<StatsData | null>(null);
  const [articles, setArticles] = useState<Article[]>([]);
  const [totalArticles, setTotalArticles] = useState(0);
  const [reportCount, setReportCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [tableLoading, setTableLoading] = useState(false);
  const [batchFetching, setBatchFetching] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);

  // ── 筛选 & 分页 & 排序 ───────────────────────────────────
  const [filter, setFilter] = useState<FilterValues>({});
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [sortField, setSortField] = useState('added_at');
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('desc');

  // ── 报道查看 ──────────────────────────────────────────────
  const [viewingReportId, setViewingReportId] = useState<string | null>(null);
  const [viewingArticle, setViewingArticle] = useState<Article | null>(null);
  const [draftArticle, setDraftArticle] = useState<Article | null>(null);
  const [draftTask, setDraftTask] = useState<{ taskId: string; articleHash: string } | null>(null);

  // ── 文章详情 Drawer ──────────────────────────────────────
  const [detailArticle, setDetailArticle] = useState<Article | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);

  // ── 加载统计数据 ─────────────────────────────────────────
  const loadStats = useCallback(async () => {
    try {
      const s = await api.getStats();
      setStats(s);
    } catch {
      // stats 非关键，静默处理
    }
  }, []);

  // ── 加载文章列表 ─────────────────────────────────────────
  const loadArticles = useCallback(async () => {
    setTableLoading(true);
    try {
      const query: ArticleQuery = {
        page,
        page_size: pageSize,
        sort_by: sortField,
        order: sortOrder,
        ...filter,
      };
      const res = await api.getArticles(query);
      setArticles(res.items);
      setTotalArticles(res.total);
    } catch {
      message.error('加载文章列表失败');
    } finally {
      setTableLoading(false);
    }
  }, [page, pageSize, sortField, sortOrder, filter]);

  // ── 加载报道总数 ─────────────────────────────────────────
  const loadReportCount = useCallback(async () => {
    try {
      const res = await api.getReports(1, 1);
      setReportCount(res.total);
    } catch {
      // 非关键
    }
  }, []);

  // ── 初始加载 ─────────────────────────────────────────────
  useEffect(() => {
    setLoading(true);
    Promise.all([loadStats(), loadReportCount()]).finally(() => setLoading(false));
  }, [loadStats, loadReportCount]);

  // ── 筛选 / 分页 / 排序变更时重新加载文章 ─────────────────
  useEffect(() => {
    loadArticles();
  }, [loadArticles]);

  // ── 自动刷新（15 秒轮询，后台全文抓取完成后 UI 自动更新）──
  useEffect(() => {
    const timer = setInterval(() => {
      // 仅在页面可见时刷新
      if (!document.hidden) {
        loadArticles();
      }
    }, 15000);
    return () => clearInterval(timer);
  }, [loadArticles]);

  // ── 流水线完成后刷新全部数据 ─────────────────────────────
  const handlePipelineComplete = useCallback(() => {
    loadStats();
    loadArticles();
    loadReportCount();
  }, [loadStats, loadArticles, loadReportCount]);

  // ── 筛选变更 → 重置到第一页 ─────────────────────────────
  const handleFilterChange = useCallback((values: FilterValues) => {
    setFilter(values);
    setPage(1);
  }, []);

  const handleCategoryClick = useCallback((category: string) => {
    setFilter((current) => ({ ...current, category }));
    setPage(1);
  }, []);

  const handleUploaded = useCallback(async () => {
    setFilter({});
    setPage(1);
    setSortField('added_at');
    setSortOrder('desc');
    await loadStats();
  }, [loadStats]);

  // ── 排序变更 ─────────────────────────────────────────────
  const handleSortChange = useCallback((field: string, order: 'asc' | 'desc' | undefined) => {
    setSortField(field);
    setSortOrder(order || 'desc');
  }, []);

  // ── 查看报道 ─────────────────────────────────────────────
  const handleViewReport = useCallback((article: Article) => {
    setViewingArticle(article);
    setViewingReportId(article.report_id);
  }, []);

  // ── 单文章 V2 打分 ──────────────────────────────────────
  const handleScoreV2Single = useCallback(
    async (article: Article) => {
      message.loading({ content: 'V2打分中...', key: 'scoresingle', duration: 0 });
      try {
        const res = await api.scoreV2Single(article.url_hash);
        message.success({
          content: `V2打分: 产品${res.product_relevance}+事件${res.event_impact}=${res.pr_total_score} ${res.is_pr_candidate ? '达标' : '未达标'}`,
          key: 'scoresingle',
          duration: 4,
        });
        loadArticles();
      } catch {
        message.error({ content: 'V2打分失败', key: 'scoresingle' });
      }
    },
    [loadArticles],
  );

  // ── 单文章 V2 流水线 ────────────────────────────────────
  const handleRunV2Single = useCallback(async (article: Article) => {
    message.loading({ content: '正在创建个性化草稿任务...', key: 'v2single', duration: 0 });
    try {
      const res = await api.runV2Single(article.url_hash);
      setDraftTask({ taskId: res.data.task_id, articleHash: article.url_hash });
      message.success({ content: '草稿任务已创建', key: 'v2single', duration: 2 });
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '未知错误';
      message.error({ content: `V2失败: ${msg}`, key: 'v2single' });
    }
  }, []);

  // ── 查看 V2 草稿 ────────────────────────────────────────
  const handleViewDrafts = useCallback((article: Article) => {
    setDraftArticle(article);
  }, []);

  // ── 查看文章详情 ────────────────────────────────────────
  const handleViewDetail = useCallback(async (article: Article) => {
    try {
      const full = await api.getArticle(article.url_hash);
      setDetailArticle(full);
    } catch {
      setDetailArticle(article); // fallback to list data
    }
    setDetailOpen(true);
  }, []);

  return (
    <div style={{ padding: '16px 24px', maxWidth: 1600, margin: '0 auto' }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: 24,
        }}
      >
        <Title level={3} style={{ margin: 0 }}>
          🚀 PR Agent Dashboard
        </Title>
        <Space>
          <Button icon={<UploadOutlined />} onClick={() => setUploadOpen(true)}>
            上传文章
          </Button>
          <Button
            icon={<ImportOutlined />}
            onClick={async () => {
              try {
                const res = await api.importWewe();
                message.success(`RSS 导入: ${res.saved} 篇`);
                loadStats();
                loadArticles();
              } catch (error: unknown) {
                message.error(`导入失败: ${getRequestErrorMessage(error)}`);
              }
            }}
          >
            导入 RSS
          </Button>
        </Space>
      </div>

      <ArticleUpload
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        onUploaded={handleUploaded}
      />

      {/* 统计卡片 */}
      <StatsCards stats={stats} loading={loading} reportCount={reportCount} />
      <TodayStatsRow stats={stats} loading={loading} />

      {/* 流水线控制 */}
      <PipelineControl onComplete={handlePipelineComplete} />

      {/* 批量补抓原文按钮 */}
      <div style={{ marginBottom: 16, display: 'flex', justifyContent: 'flex-end' }}>
        <Button
          loading={batchFetching}
          onClick={async () => {
            setBatchFetching(true);
            try {
              const res = await api.batchFetchContent();
              if (res.data.updated > 0) {
                message.success(`补抓完成：${res.data.updated}/${res.data.total} 篇文章已更新`);
                loadArticles();
              } else {
                message.info('没有需要补抓的文章');
              }
            } catch {
              message.error('批量补抓失败');
            }
            setBatchFetching(false);
          }}
        >
          批量补抓原文
        </Button>
      </div>

      {draftTask && (
        <div style={{ marginBottom: 16 }}>
          <PipelineTaskProgress
            taskId={draftTask.taskId}
            onCompleted={async () => {
              const article = await api.getArticle(draftTask.articleHash);
              setArticles((current) =>
                current.map((item) => (item.url_hash === article.url_hash ? article : item)),
              );
              setDraftArticle(article);
              setDraftTask(null);
              message.success('个性化草稿已生成');
            }}
            onFailed={(task) => {
              setDraftTask(null);
              message.error(`草稿生成失败：${task.error || '未知错误'}`);
            }}
          />
        </div>
      )}

      {/* 筛选栏 */}
      <FilterBar
        value={filter}
        onChange={handleFilterChange}
        categories={stats?.category_distribution ? Object.keys(stats.category_distribution) : []}
      />

      <Row style={{ marginBottom: 16 }}>
        <Col span={24}>
          <CategoryBreakdown
            distribution={stats?.category_distribution ?? {}}
            loading={loading}
            onCategoryClick={handleCategoryClick}
          />
        </Col>
      </Row>

      <Row gutter={[16, 16]} align="stretch">
        {/* 文章表格 */}
        <Col xs={24} sm={24} lg={16} data-testid="article-table-column">
          <ArticleTable
            articles={articles}
            total={totalArticles}
            loading={tableLoading}
            page={page}
            pageSize={pageSize}
            onPageChange={(p, ps) => {
              setPage(p);
              setPageSize(ps);
            }}
            onSortChange={handleSortChange}
            onViewReport={handleViewReport}
            onViewDetail={handleViewDetail}
            onViewDrafts={handleViewDrafts}
            onRunV2Single={handleRunV2Single}
            onScoreV2Single={handleScoreV2Single}
            onRefresh={loadArticles}
          />
        </Col>
        <Col xs={24} sm={24} lg={8} data-testid="hot-ranking-column">
          <HotRankingPanel />
        </Col>
      </Row>

      {/* 报道查看器 Modal */}
      <ReportViewer
        reportId={viewingReportId}
        article={viewingArticle}
        onClose={() => {
          setViewingReportId(null);
          setViewingArticle(null);
        }}
      />

      {/* V2 PR 草稿查看器 Modal */}
      {draftArticle && <DraftViewer article={draftArticle} onClose={() => setDraftArticle(null)} />}

      {/* 文章详情 Drawer */}
      <Drawer title="文章详情" open={detailOpen} onClose={() => setDetailOpen(false)} width={640}>
        {detailArticle && (
          <>
            <Paragraph>
              <Text strong>标题：</Text>
              <a href={detailArticle.url} target="_blank" rel="noopener noreferrer">
                {detailArticle.title}
              </a>
            </Paragraph>
            <Paragraph>
              <Text strong>来源：</Text>
              <Tag color="blue">{detailArticle.source}</Tag>
            </Paragraph>
            <Paragraph>
              <Text strong>分类：</Text>
              <Tag>{detailArticle.category || '未分类'}</Tag>
            </Paragraph>
            <Space>
              <Text strong>AI相关度：</Text>
              <Tag color="green">{detailArticle.ai_relevance_score}</Tag>
              <Text strong>可报道性：</Text>
              <Tag color="purple">{detailArticle.reportability_score}</Tag>
              <Text strong>综合分：</Text>
              <Tag color={detailArticle.total_score >= 140 ? 'red' : 'default'}>
                {detailArticle.total_score}
              </Tag>
            </Space>
            <Paragraph style={{ marginTop: 16 }}>
              <Text strong>打分理由：</Text>
              {detailArticle.score_reason || '无'}
            </Paragraph>
            {detailArticle.summary_cn && (
              <Paragraph>
                <Text strong>AI 摘要：</Text>
                {detailArticle.summary_cn}
              </Paragraph>
            )}
            {detailArticle.content_md && (
              <>
                <Paragraph style={{ marginTop: 16 }}>
                  <Text strong>原文内容（Markdown）：</Text>
                </Paragraph>
                <div
                  style={{
                    maxHeight: 400,
                    overflow: 'auto',
                    padding: 12,
                    background: '#fafafa',
                    borderRadius: 8,
                  }}
                >
                  <pre
                    style={{
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word',
                      margin: 0,
                    }}
                  >
                    {detailArticle.content_md.slice(0, 5000)}
                  </pre>
                </div>
              </>
            )}
          </>
        )}
      </Drawer>
    </div>
  );
}
