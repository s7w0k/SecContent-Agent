/**
 * 信息搜索页面（SearXNG 网络搜索）
 *
 * 提供关键词搜索、结果选择、导入文章库功能。
 * 搜索功能依赖后端 SearXNG 集成，未启用时显示提示。
 */

import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Modal,
  Select,
  Space,
  Spin,
  Statistic,
  Tag,
  Typography,
  message,
} from 'antd';
import { useCallback, useEffect, useState } from 'react';
import api from '../api/client';
import SearchResultList from '../components/SearchResultList';
import type {
  SearchImportResponse,
  SearchStatusResponse,
  SearchWarning,
  WebSearchResult,
} from '../types';

const { Title, Text } = Typography;

interface WebSearchPageProps {
  onNavigate: (page: string) => void;
}

interface HttpLikeError {
  response?: {
    status?: number;
    data?: {
      detail?: string;
    };
  };
  code?: string;
  message?: string;
}

function getErrorMessage(error: unknown, fallback: string): string {
  const maybeError = error as HttpLikeError;
  return maybeError.response?.data?.detail || maybeError.message || fallback;
}

const STATUS_TAG_MAP: Record<string, { color: string; label: string }> = {
  imported: { color: 'success', label: '已导入' },
  duplicate: { color: 'blue', label: '重复' },
  invalid_url: { color: 'warning', label: '无效链接' },
  failed: { color: 'error', label: '失败' },
};

export default function WebSearchPage({ onNavigate }: WebSearchPageProps) {
  const [searchId, setSearchId] = useState<string | null>(null);
  const [results, setResults] = useState<WebSearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<SearchImportResponse | null>(null);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [warnings, setWarnings] = useState<SearchWarning[]>([]);
  const [searchStatus, setSearchStatus] = useState<SearchStatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);

  // 表单状态
  const [keyword, setKeyword] = useState('');
  const [category, setCategory] = useState<string>('general');
  const [language, setLanguage] = useState<string>('all');
  const [timeRange, setTimeRange] = useState<string>('');
  const [safesearch, setSafesearch] = useState<number>(1);

  const maxImportItems = searchStatus?.max_import_items ?? 20;

  // 挂载时检查搜索服务状态 + 恢复上次搜索会话
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const status = await api.webSearchApi.getStatus();
        if (!cancelled) {
          setSearchStatus(status);
        }
      } catch (err) {
        if (!cancelled) {
          setError(getErrorMessage(err, '无法获取搜索服务状态'));
        }
      } finally {
        if (!cancelled) {
          setStatusLoading(false);
        }
      }

      // 尝试恢复上次搜索会话
      const saved = sessionStorage.getItem('web_search_session');
      if (!saved || cancelled) return;
      try {
        const {
          searchId: sid,
          keyword: kw,
          category: cat,
          language: lang,
          timeRange: tr,
          safesearch: ss,
        } = JSON.parse(saved);
        if (!sid) return;
        const resp = await api.webSearchApi.getSession(sid);
        if (cancelled) return;
        setSearchId(resp.search_id);
        setResults(resp.results);
        setHasMore(resp.has_more);
        setPage(resp.page);
        setWarnings(resp.warnings || []);
        if (kw) setKeyword(kw);
        if (cat) setCategory(cat);
        if (lang) setLanguage(lang);
        if (tr) setTimeRange(tr);
        if (ss !== undefined) setSafesearch(ss);
      } catch {
        // 会话已过期或恢复失败，清除存储
        sessionStorage.removeItem('web_search_session');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const doSearch = useCallback(
    async (pageNum: number) => {
      const q = keyword.trim();
      if (q.length < 2 || q.length > 200) {
        message.warning('关键词长度需在 2-200 字符之间');
        return;
      }
      setLoading(true);
      setError(null);
      setImportResult(null);
      try {
        const params: {
          q: string;
          categories?: string[];
          language?: string;
          time_range?: string;
          safesearch: number;
          pageno: number;
        } = {
          q,
          safesearch,
          pageno: pageNum,
        };
        if (category) params.categories = [category];
        if (language !== 'all') params.language = language;
        if (timeRange) params.time_range = timeRange;

        const resp = await api.webSearchApi.search(params);
        setSearchId(resp.search_id);
        setResults(resp.results);
        setHasMore(resp.has_more);
        setPage(resp.page);
        setWarnings(resp.warnings || []);
        setSelectedIds(new Set());
        // 持久化到 sessionStorage，切换 tab 后可恢复
        sessionStorage.setItem(
          'web_search_session',
          JSON.stringify({
            searchId: resp.search_id,
            keyword: q,
            category,
            language,
            timeRange,
            safesearch,
          }),
        );
      } catch (err) {
        const maybeError = err as HttpLikeError;
        const status = maybeError.response?.status;
        if (status === 429) {
          setError('搜索请求过于频繁，请稍后再试');
        } else if (maybeError.code === 'ECONNABORTED' || maybeError.message?.includes('timeout')) {
          setError('搜索超时，请稍后重试');
        } else if (status === 503) {
          setError('搜索服务暂不可用');
        } else {
          setError(getErrorMessage(err, '搜索失败，请稍后重试'));
        }
        setResults([]);
        setWarnings([]);
        setSearchId(null);
        sessionStorage.removeItem('web_search_session');
      } finally {
        setLoading(false);
      }
    },
    [keyword, category, language, timeRange, safesearch],
  );

  const handleSearch = useCallback(() => {
    doSearch(1);
  }, [doSearch]);

  const handleToggle = useCallback((resultId: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(resultId)) {
        next.delete(resultId);
      } else {
        next.add(resultId);
      }
      return next;
    });
  }, []);

  const handleSelectAll = useCallback(() => {
    setSelectedIds((prev) => {
      const available = results.filter((r) => !r.is_imported);
      const allSelected = available.every((r) => prev.has(r.result_id));
      if (allSelected && prev.size > 0) {
        return new Set();
      }
      const next = new Set<string>();
      for (const r of available) {
        if (next.size >= maxImportItems) break;
        next.add(r.result_id);
      }
      return next;
    });
  }, [results, maxImportItems]);

  const handlePrevPage = useCallback(() => {
    if (page > 1) {
      doSearch(page - 1);
    }
  }, [page, doSearch]);

  const handleNextPage = useCallback(() => {
    if (hasMore) {
      doSearch(page + 1);
    }
  }, [page, hasMore, doSearch]);

  const handleImport = useCallback(() => {
    if (!searchId || selectedIds.size === 0) return;
    const count = selectedIds.size;
    Modal.confirm({
      title: '确认导入',
      content: `确认将选中的 ${count} 条搜索结果导入文章库？导入后将自动进入富化流程。`,
      okText: '导入',
      cancelText: '取消',
      onOk: async () => {
        setImporting(true);
        setError(null);
        try {
          const idempotencyKey = crypto.randomUUID();
          const result = await api.webSearchApi.importResults(
            searchId,
            [...selectedIds],
            idempotencyKey,
          );
          setImportResult(result);
          message.success(`成功导入 ${result.summary.imported} 条结果`);
          // 标记已导入的结果
          setResults((prev) =>
            prev.map((r) => (selectedIds.has(r.result_id) ? { ...r, is_imported: true } : r)),
          );
          setSelectedIds(new Set());
        } catch (err) {
          setError(getErrorMessage(err, '导入失败，请稍后重试'));
        } finally {
          setImporting(false);
        }
      },
    });
  }, [searchId, selectedIds]);

  const availableCount = results.filter((r) => !r.is_imported).length;
  const allAvailableSelected =
    availableCount > 0 &&
    results.filter((r) => !r.is_imported).every((r) => selectedIds.has(r.result_id));

  // ── 状态加载中 ──
  if (statusLoading) {
    return (
      <div style={{ padding: 48, textAlign: 'center' }}>
        <Spin size="large" />
      </div>
    );
  }

  // ── 搜索功能未启用 ──
  if (!searchStatus?.enabled) {
    return (
      <div style={{ padding: 24, maxWidth: 800, margin: '0 auto' }}>
        <Title level={3}>信息搜索</Title>
        <Alert
          type="warning"
          message="搜索功能未启用"
          description="管理员尚未启用 SearXNG 网络搜索功能（WEB_SEARCH_ENABLED=false）。请联系管理员开启后使用。"
          showIcon
        />
      </div>
    );
  }

  // ── 主界面 ──
  return (
    <div style={{ padding: 24, maxWidth: 1000, margin: '0 auto' }}>
      <Title level={3}>信息搜索</Title>

      {!searchStatus.available && (
        <Alert
          type="error"
          message="搜索服务不可用"
          description="SearXNG 搜索引擎当前不可用，请稍后再试。"
          showIcon
          style={{ marginBottom: 16 }}
        />
      )}

      {error && (
        <Alert
          type="error"
          message={error}
          showIcon
          closable
          onClose={() => setError(null)}
          style={{ marginBottom: 16 }}
        />
      )}

      <Card style={{ marginBottom: 16 }}>
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
          <Input.Search
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onSearch={() => handleSearch()}
            placeholder="输入搜索关键词（2-200 字符）"
            maxLength={200}
            enterButton={
              <Button
                type="primary"
                disabled={!keyword.trim() || loading || !searchStatus.available}
                loading={loading}
              >
                搜索
              </Button>
            }
          />
          <Space wrap>
            <Select
              value={category}
              onChange={(v) => setCategory(v as string)}
              style={{ width: 130 }}
              options={[
                { value: 'general', label: '综合' },
                { value: 'news', label: '新闻' },
              ]}
            />
            <Select
              value={language}
              onChange={(v) => setLanguage(v as string)}
              style={{ width: 130 }}
              options={[
                { value: 'all', label: '全部语言' },
                { value: 'zh-CN', label: '中文' },
                { value: 'en', label: '英文' },
              ]}
            />
            <Select
              value={timeRange}
              onChange={(v) => setTimeRange(v as string)}
              style={{ width: 140 }}
              options={[
                { value: '', label: '不限时间' },
                { value: 'day', label: '一天内' },
                { value: 'month', label: '一个月内' },
                { value: 'year', label: '一年内' },
              ]}
            />
            <Select
              value={safesearch}
              onChange={(v) => setSafesearch(v as number)}
              style={{ width: 130 }}
              options={[
                { value: 0, label: '安全搜索：关闭' },
                { value: 1, label: '安全搜索：中等' },
                { value: 2, label: '安全搜索：严格' },
              ]}
            />
          </Space>
        </Space>
      </Card>

      {warnings.length > 0 && (
        <Space direction="vertical" style={{ width: '100%', marginBottom: 16 }}>
          {warnings.map((w, i) => (
            <Alert
              key={`${w.code}-${i}`}
              type="warning"
              message={`${w.message}（${w.count} 个引擎）`}
              showIcon
            />
          ))}
        </Space>
      )}

      {importResult && (
        <Card title="导入结果" style={{ marginBottom: 16 }}>
          <Space direction="vertical" style={{ width: '100%' }} size="middle">
            <Space size="large" wrap>
              <Statistic title="请求" value={importResult.summary.requested} />
              <Statistic
                title="导入成功"
                value={importResult.summary.imported}
                valueStyle={{ color: '#3f8600' }}
              />
              <Statistic title="重复" value={importResult.summary.duplicate} />
              <Statistic
                title="失败"
                value={importResult.summary.failed}
                valueStyle={{ color: '#cf1322' }}
              />
              {importResult.summary.enrichment_queued > 0 && (
                <Statistic title="富化排队" value={importResult.summary.enrichment_queued} />
              )}
            </Space>
            <Space direction="vertical" style={{ width: '100%' }} size="small">
              {importResult.items.map((item) => {
                const tagInfo = STATUS_TAG_MAP[item.status] || {
                  color: 'default',
                  label: item.status,
                };
                return (
                  <div
                    key={item.result_id}
                    style={{ display: 'flex', alignItems: 'center', gap: 8 }}
                  >
                    <Tag color={tagInfo.color}>{tagInfo.label}</Tag>
                    <Text style={{ fontSize: 13 }}>{item.message}</Text>
                  </div>
                );
              })}
            </Space>
            <Button type="primary" onClick={() => onNavigate('dashboard')}>
              去 Dashboard 查看
            </Button>
          </Space>
        </Card>
      )}

      {searchId && results.length > 0 && (
        <>
          <Card size="small" style={{ marginBottom: 12 }}>
            <Space style={{ width: '100%', justifyContent: 'space-between' }} wrap>
              <Space>
                <Button size="small" onClick={handleSelectAll} disabled={availableCount === 0}>
                  {allAvailableSelected ? '取消全选' : '全选'}
                </Button>
                <Text type="secondary">
                  已选 {selectedIds.size} / {maxImportItems}（可选 {availableCount} 条）
                </Text>
              </Space>
              <Button
                type="primary"
                size="small"
                onClick={handleImport}
                disabled={selectedIds.size === 0 || importing}
                loading={importing}
              >
                导入选中结果
              </Button>
            </Space>
          </Card>

          <SearchResultList
            results={results}
            selectedIds={selectedIds}
            onToggle={handleToggle}
            maxSelection={maxImportItems}
          />

          <div style={{ marginTop: 16, textAlign: 'center' }}>
            <Space>
              <Button disabled={page <= 1 || loading} onClick={handlePrevPage}>
                上一页
              </Button>
              <Text type="secondary">第 {page} 页</Text>
              <Button disabled={!hasMore || loading} onClick={handleNextPage}>
                下一页
              </Button>
            </Space>
          </div>
        </>
      )}

      {searchId && results.length === 0 && !loading && (
        <Empty description="未找到相关结果，请尝试更换关键词" />
      )}

      {!searchId && !importResult && !error && <Empty description="请输入关键词开始搜索" />}
    </div>
  );
}
