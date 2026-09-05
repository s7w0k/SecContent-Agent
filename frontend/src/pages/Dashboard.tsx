/**
 * 仪表盘主页面
 *
 * 组合 StatsCards + FilterBar + ArticleTable + PipelineControl + ReportViewer，
 * 管理全局状态（筛选/分页/排序/报道查看）。
 */

import { ImportOutlined, UploadOutlined } from '@ant-design/icons';
import {
  Button,
  Checkbox,
  Col,
  Drawer,
  Modal,
  Row,
  Segmented,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import { useCallback, useEffect, useRef, useState } from 'react';
import api, { manuscriptApi, userKnowledgeApi, type Manuscript } from '../api/client';
import ArticleTable from '../components/ArticleTable';
import ArticleUpload from '../components/ArticleUpload';
import DraftViewer from '../components/DraftViewer';
import FilterBar from '../components/FilterBar';
import PipelineControl from '../components/PipelineControl';
import PipelineTaskProgress from '../components/PipelineTaskProgress';
import ReportViewer from '../components/ReportViewer';
import StatsCards from '../components/StatsCards';
import TodayStatsRow from '../components/TodayStatsRow';
import GenerationConfigModal, {
  type GenerationConfig,
} from '../components/pipeline/GenerationConfigModal';
import { useActiveTasks } from '../hooks/useActiveTasks';
import type { Article, ArticleQuery, FilterValues, SourceType, StatsData } from '../types';

const { Title, Paragraph, Text } = Typography;

function getRequestErrorMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } }).response;
    if (typeof response?.data?.detail === 'string') return response.data.detail;
  }
  return error instanceof Error ? error.message : '未知错误';
}

function formatTime(v?: string): string {
  if (!v) return '—';
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('zh-CN', { hour12: false });
}

interface DashboardProps {
  initialSourceType?: string;
  refreshKey?: number;
}

export default function Dashboard({ initialSourceType, refreshKey }: DashboardProps) {
  // ── 数据状态 ──────────────────────────────────────────────
  const [stats, setStats] = useState<StatsData | null>(null);
  const [articles, setArticles] = useState<Article[]>([]);
  // 我的稿件库：列表展示，点击题目查看文章全文
  const [manuscripts, setManuscripts] = useState<Manuscript[]>([]);
  const [msLoading, setMsLoading] = useState(false);
  const [viewOpen, setViewOpen] = useState(false);
  const [viewTitle, setViewTitle] = useState('');
  const [viewContent, setViewContent] = useState('');
  const [totalArticles, setTotalArticles] = useState(0);
  const [reportCount, setReportCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [tableLoading, setTableLoading] = useState(false);
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
  // 文章展示区上方切换：新闻 / 稿件
  const [viewMode, setViewMode] = useState<'news' | 'manuscript'>('news');

  // ── 页面重新挂载时恢复进行中的任务 ────────────────────────
  const { draftTask: restoredDraftTask } = useActiveTasks();
  const draftRestoredRef = useRef(false);
  useEffect(() => {
    if (restoredDraftTask && !draftTask && !draftRestoredRef.current) {
      draftRestoredRef.current = true;
      setDraftTask(restoredDraftTask);
    }
  }, [restoredDraftTask, draftTask]);

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

  // ── 加载稿件库（资料库·稿件栏） ─────────────────────────
  const loadManuscripts = useCallback(async () => {
    setMsLoading(true);
    try {
      const items = await manuscriptApi.list();
      setManuscripts(items ?? []);
    } catch {
      message.error('加载稿件库失败');
    } finally {
      setMsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadManuscripts();
  }, [loadManuscripts]);

  // ── 查看稿件全文 / 下载 ─────────────────────────────────
  const openManuscript = async (m: Manuscript) => {
    try {
      const full = await manuscriptApi.get(m.manuscript_id);
      setViewTitle(m.title);
      setViewContent(full.content_md || '（空内容）');
      setViewOpen(true);
    } catch {
      message.error('加载稿件内容失败');
    }
  };

  const handleDownloadMs = () => {
    if (!viewContent) return;
    const blob = new Blob([viewContent], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${(viewTitle || '稿件').replace(/[\\/:*?"<>|]/g, '_')}.md`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

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

  // ── 从搜索页跳转时设置来源筛选 ───────────────────────────
  useEffect(() => {
    if (initialSourceType) {
      setFilter((prev) => ({ ...prev, source_type: initialSourceType as SourceType }));
      setPage(1);
      setSortField('added_at');
      setSortOrder('desc');
    }
  }, [initialSourceType]);

  // ── refreshKey 变更时触发数据重新加载 ─────────────────────
  useEffect(() => {
    if (refreshKey !== undefined && refreshKey > 0) {
      loadArticles();
    }
  }, [refreshKey, loadArticles]);

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

  // ── 筛选变更 -> 重置到第一页 ─────────────────────────────
  const handleFilterChange = useCallback((values: FilterValues) => {
    setFilter(values);
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

  // ── 单文章 V2 打分（带产品选择弹窗） ──────────────────────
  const [scoreModalArticle, setScoreModalArticle] = useState<Article | null>(null);
  const [scoreProducts, setScoreProducts] = useState<{ product_id: string; name: string }[]>([]);
  const [scoreSelectedIds, setScoreSelectedIds] = useState<string[]>([]);
  const [scoreLoading, setScoreLoading] = useState(false);

  const handleScoreV2Single = useCallback(async (article: Article) => {
    setScoreModalArticle(article);
    setScoreSelectedIds([]);
    try {
      const items = await userKnowledgeApi.listProducts();
      const enabled = items.filter((p) => p.enabled);
      setScoreProducts(enabled.map((p) => ({ product_id: p.product_id, name: p.name })));
      setScoreSelectedIds(enabled.map((p) => p.product_id));
    } catch {
      message.error('加载产品列表失败');
    }
  }, []);

  const handleScoreConfirm = useCallback(async () => {
    if (!scoreModalArticle) return;
    if (scoreSelectedIds.length === 0) {
      message.warning('请至少选择一个产品');
      return;
    }
    setScoreLoading(true);
    message.loading({ content: 'V2打分中...', key: 'scoresingle', duration: 0 });
    try {
      const res = await api.scoreV2Single(scoreModalArticle.url_hash, scoreSelectedIds);
      const topProduct =
        res.product_scores?.length > 0
          ? [...res.product_scores].sort((a, b) => b.score - a.score)[0]
          : null;
      const productInfo = topProduct
        ? ` | 最相关: ${topProduct.product_name}(${topProduct.score})`
        : '';
      message.success({
        content: `V2打分: 产品${res.product_relevance}+事件${res.event_impact}=${res.pr_total_score} ${res.is_pr_candidate ? '达标' : '未达标'}${productInfo}`,
        key: 'scoresingle',
        duration: 5,
      });
      setScoreModalArticle(null);
      loadArticles();
    } catch {
      message.error({ content: 'V2打分失败', key: 'scoresingle' });
    } finally {
      setScoreLoading(false);
    }
  }, [scoreModalArticle, scoreSelectedIds, loadArticles]);

  // ── 单文章 V2 流水线 ────────────────────────────────────
  const [templateModalArticle, setTemplateModalArticle] = useState<Article | null>(null);
  const [genConfigOpen, setGenConfigOpen] = useState(false);
  const [genConfigLoading, setGenConfigLoading] = useState(false);

  const handleRunV2Single = useCallback((article: Article) => {
    setTemplateModalArticle(article);
    setGenConfigOpen(true);
  }, []);

  const handleConfirmGenerate = useCallback(
    async (config: GenerationConfig) => {
      const article = templateModalArticle;
      if (!article) return;
      setGenConfigLoading(true);
      message.loading({ content: '正在创建个性化草稿任务...', key: 'v2single', duration: 0 });
      try {
        const res = await api.runV2Single(article.url_hash, config.reference_template, {
          product_target_mode: config.product_target_mode,
          selected_product_ids: config.selected_product_ids,
          product_relevance_enabled: config.product_relevance_enabled,
          force_generate: config.force_generate,
          draft_variants: config.draft_variants,
        });
        setDraftTask({ taskId: res.data.task_id, articleHash: article.url_hash });
        message.success({ content: '草稿任务已创建', key: 'v2single', duration: 2 });
        setGenConfigOpen(false);
        setTemplateModalArticle(null);
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : '未知错误';
        message.error({ content: `V2失败: ${msg}`, key: 'v2single' });
      } finally {
        setGenConfigLoading(false);
      }
    },
    [templateModalArticle],
  );

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

      {/* 文章展示区上方：新闻 / 稿件 切换 */}
      <div style={{ marginBottom: 16 }}>
        <Segmented
          value={viewMode}
          onChange={(v) => setViewMode(v as 'news' | 'manuscript')}
          options={[
            { label: '新闻', value: 'news' },
            { label: '稿件', value: 'manuscript' },
          ]}
        />
      </div>

      {viewMode === 'news' ? (
        <>
          {/* 筛选栏 */}
          <FilterBar
            value={filter}
            onChange={handleFilterChange}
            categories={
              stats?.category_distribution ? Object.keys(stats.category_distribution) : []
            }
          />

          <Row style={{ marginBottom: 16 }}>
            <Col span={24} data-testid="article-table-column">
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
          </Row>
        </>
      ) : (
        <Row style={{ marginBottom: 16 }}>
          <Col span={24}>
            <Table<Manuscript>
              rowKey="manuscript_id"
              loading={msLoading}
              dataSource={manuscripts}
              pagination={{ pageSize: 10, showSizeChanger: true, showTotal: (t) => `共 ${t} 篇` }}
              locale={{ emptyText: '稿件库为空，可先在 Agent 工作台保存或上传一份稿件' }}
              bordered
              style={{ background: '#fff', borderRadius: 12, overflow: 'hidden' }}
              columns={[
                {
                  title: '稿件题目',
                  dataIndex: 'title',
                  ellipsis: true,
                  render: (value: string, record: Manuscript) => (
                    <a onClick={() => openManuscript(record)} style={{ color: '#2563eb' }}>
                      {value}
                    </a>
                  ),
                },
                {
                  title: '对应新闻题目',
                  dataIndex: 'news_title',
                  ellipsis: true,
                  render: (value?: string) => value || <span style={{ color: '#bbb' }}>—</span>,
                },
                {
                  title: '保存时间',
                  width: 200,
                  render: (_: unknown, record: Manuscript) =>
                    formatTime(record.created_at || record.updated_at),
                },
              ]}
            />
          </Col>
        </Row>
      )}

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

      {/* 生成草稿弹窗（产品选择 + 参考稿上传） */}
      <GenerationConfigModal
        open={genConfigOpen}
        onCancel={() => {
          setGenConfigOpen(false);
          setTemplateModalArticle(null);
        }}
        onConfirm={handleConfirmGenerate}
        loading={genConfigLoading}
        articleScore={templateModalArticle?.pr_total_score}
      />

      {/* V2 打分产品选择弹窗 */}
      <Modal
        title="V2 打分 - 选择产品"
        open={!!scoreModalArticle}
        onCancel={() => setScoreModalArticle(null)}
        onOk={handleScoreConfirm}
        confirmLoading={scoreLoading}
        okText="开始打分"
        cancelText="取消"
      >
        <div style={{ marginBottom: 8, color: '#666' }}>请选择要对哪些产品进行相关性评分：</div>
        <Checkbox.Group
          value={scoreSelectedIds}
          onChange={(values) => setScoreSelectedIds(values as string[])}
          style={{ display: 'flex', flexDirection: 'column', gap: 8 }}
        >
          {scoreProducts.map((p) => (
            <Checkbox key={p.product_id} value={p.product_id}>
              {p.name}
            </Checkbox>
          ))}
        </Checkbox.Group>
      </Modal>

      {/* 稿件全文查看弹窗 */}
      <Modal
        title={viewTitle}
        open={viewOpen}
        onCancel={() => setViewOpen(false)}
        footer={[
          <Button key="download" type="primary" onClick={handleDownloadMs}>
            下载 md
          </Button>,
          <Button key="close" onClick={() => setViewOpen(false)}>
            关闭
          </Button>,
        ]}
        width={720}
      >
        <div
          style={{
            maxHeight: '62vh',
            overflow: 'auto',
            whiteSpace: 'pre-wrap',
            fontSize: 14,
            lineHeight: 1.75,
            color: '#333',
            background: '#fafbfc',
            borderRadius: 8,
            padding: 14,
          }}
        >
          {viewContent}
        </div>
      </Modal>
    </div>
  );
}
