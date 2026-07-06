/**
 * 仪表盘主页面
 *
 * 组合 StatsCards + FilterBar + ArticleTable + PipelineControl + ReportViewer，
 * 管理全局状态（筛选/分页/排序/报道查看）。
 */

import { useCallback, useEffect, useState } from "react";
import { Button, Drawer, message, Space, Tag, Typography } from "antd";
import { ImportOutlined } from "@ant-design/icons";
import StatsCards from "../components/StatsCards";
import FilterBar from "../components/FilterBar";
import ArticleTable from "../components/ArticleTable";
import PipelineControl from "../components/PipelineControl";
import ReportViewer from "../components/ReportViewer";
import DraftViewer from "../components/DraftViewer";
import CrawlConfig from "../components/CrawlConfig";
import api from "../api/client";
import type {
  Article,
  ArticleQuery,
  FilterValues,
  StatsData,
} from "../types";

const { Title, Paragraph, Text } = Typography;

export default function Dashboard() {
  // ── 数据状态 ──────────────────────────────────────────────
  const [stats, setStats] = useState<StatsData | null>(null);
  const [articles, setArticles] = useState<Article[]>([]);
  const [totalArticles, setTotalArticles] = useState(0);
  const [reportCount, setReportCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [tableLoading, setTableLoading] = useState(false);

  // ── 筛选 & 分页 & 排序 ───────────────────────────────────
  const [filter, setFilter] = useState<FilterValues>({});
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [sortField, setSortField] = useState("added_at");
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("desc");

  // ── 报道查看 ──────────────────────────────────────────────
  const [viewingReportId, setViewingReportId] = useState<string | null>(null);
  const [viewingArticle, setViewingArticle] = useState<Article | null>(null);
  const [draftArticle, setDraftArticle] = useState<Article | null>(null);

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
      message.error("加载文章列表失败");
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
    Promise.all([loadStats(), loadArticles(), loadReportCount()]).finally(() =>
      setLoading(false),
    );
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ── 筛选 / 分页 / 排序变更时重新加载文章 ─────────────────
  useEffect(() => {
    loadArticles();
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

  // ── 排序变更 ─────────────────────────────────────────────
  const handleSortChange = useCallback(
    (field: string, order: "asc" | "desc" | undefined) => {
      setSortField(field);
      setSortOrder(order || "desc");
    },
    [],
  );

  // ── 查看报道 ─────────────────────────────────────────────
  const handleViewReport = useCallback((article: Article) => {
    setViewingArticle(article);
    setViewingReportId(article.report_id);
  }, []);

  // ── 单文章 V2 打分 ──────────────────────────────────────
  const handleScoreV2Single = useCallback(async (article: Article) => {
    message.loading({ content: "V2打分中...", key: "scoresingle", duration: 0 });
    try {
      const res = await api.scoreV2Single(article.url_hash);
      message.success({
        content: `V2打分: 产品${res.product_relevance}+事件${res.event_impact}=${res.pr_total_score} ${res.is_pr_candidate ? "达标" : "未达标"}`,
        key: "scoresingle", duration: 4,
      });
      loadArticles();
    } catch (e: unknown) {
      message.error({ content: "V2打分失败", key: "scoresingle" });
    }
  }, [loadArticles]);

  // ── 单文章 V2 流水线 ────────────────────────────────────
  const handleRunV2Single = useCallback(async (article: Article) => {
    message.loading({ content: "V2流水线运行中...", key: "v2single", duration: 0 });
    try {
      const res = await api.runV2Single(article.url_hash);
      const steps = res.steps.map((s) => {
        if (s.phase === "classify_v2") return `分类:${s.category}`;
        if (s.phase === "score_v2") return `综合分:${s.pr_total_score}`;
        if (s.phase === "draft") return `草稿:${s.draft_count}篇`;
        return s.phase;
      }).join(" → ");
      message.success({ content: `V2完成: ${steps}`, key: "v2single", duration: 5 });
      loadArticles();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "未知错误";
      message.error({ content: `V2失败: ${msg}`, key: "v2single" });
    }
  }, [loadArticles]);

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
    <div style={{ padding: "16px 24px", maxWidth: 1600, margin: "0 auto" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 24 }}>
        <Title level={3} style={{ margin: 0 }}>
          🚀 PR Agent Dashboard
        </Title>
        <Button icon={<ImportOutlined />}
          onClick={async () => {
            try {
              const res = await api.importWewe();
              message.success(`RSS 导入: ${res.saved} 篇`);
              loadStats(); loadArticles();
            } catch (e: any) { message.error(`导入失败: ${e?.response?.data?.detail || e.message}`); }
          }}>导入 RSS</Button>
      </div>

      {/* API 抓取配置 */}
      <CrawlConfig onCrawl={() => { loadStats(); loadArticles(); loadReportCount(); }} />

      {/* 统计卡片 */}
      <StatsCards stats={stats} loading={loading} reportCount={reportCount} />

      {/* 流水线控制 */}
      <PipelineControl onComplete={handlePipelineComplete} />

      {/* 筛选栏 */}
      <FilterBar
        value={filter}
        onChange={handleFilterChange}
        categories={
          stats?.category_distribution
            ? Object.keys(stats.category_distribution)
            : []
        }
      />

      {/* 文章表格 */}
      <ArticleTable
        articles={articles}
        total={totalArticles}
        loading={tableLoading}
        page={page}
        pageSize={pageSize}
        onPageChange={(p, ps) => { setPage(p); setPageSize(ps); }}
        onSortChange={handleSortChange}
        onViewReport={handleViewReport}
        onViewDetail={handleViewDetail}
        onViewDrafts={handleViewDrafts}
        onRunV2Single={handleRunV2Single}
        onScoreV2Single={handleScoreV2Single}
        onRefresh={loadArticles}
      />

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
      {draftArticle && (
        <DraftViewer
          article={draftArticle}
          onClose={() => setDraftArticle(null)}
        />
      )}

      {/* 文章详情 Drawer */}
      <Drawer
        title="文章详情"
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        width={640}
      >
        {detailArticle && (
          <>
            <Paragraph>
              <Text strong>标题：</Text>
              <a
                href={detailArticle.url}
                target="_blank"
                rel="noopener noreferrer"
              >
                {detailArticle.title}
              </a>
            </Paragraph>
            <Paragraph>
              <Text strong>来源：</Text>
              <Tag color="blue">{detailArticle.source}</Tag>
            </Paragraph>
            <Paragraph>
              <Text strong>分类：</Text>
              <Tag>{detailArticle.category || "未分类"}</Tag>
            </Paragraph>
            <Space>
              <Text strong>AI相关度：</Text>
              <Tag color="green">{detailArticle.ai_relevance_score}</Tag>
              <Text strong>可报道性：</Text>
              <Tag color="purple">{detailArticle.reportability_score}</Tag>
              <Text strong>综合分：</Text>
              <Tag
                color={
                  detailArticle.total_score >= 140 ? "red" : "default"
                }
              >
                {detailArticle.total_score}
              </Tag>
            </Space>
            <Paragraph style={{ marginTop: 16 }}>
              <Text strong>打分理由：</Text>
              {detailArticle.score_reason || "无"}
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
                    overflow: "auto",
                    padding: 12,
                    background: "#fafafa",
                    borderRadius: 8,
                  }}
                >
                  <pre
                    style={{
                      whiteSpace: "pre-wrap",
                      wordBreak: "break-word",
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
