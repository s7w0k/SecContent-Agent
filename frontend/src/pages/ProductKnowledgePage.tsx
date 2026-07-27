/**
 * 产品知识库页面（K.3）
 *
 * 全员可见的只读浏览页面：左侧目录树 + 搜索，右侧文档查看器，
 * 顶部展示知识库加载状态。所有登录用户均可访问。
 */

import { ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Input,
  List,
  Row,
  Skeleton,
  Space,
  Spin,
  Tag,
  Typography,
  message,
} from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { knowledgeApi } from '../api/client';
import KnowledgeTree from '../components/KnowledgeTree';
import KnowledgeUsageBadge from '../components/KnowledgeUsageBadge';
import KnowledgeViewer from '../components/KnowledgeViewer';
import type {
  KnowledgeDocument,
  KnowledgeSearchResult,
  KnowledgeStatus,
  KnowledgeTreeNode,
  KnowledgeUsageItem,
} from '../types';

const { Paragraph, Text, Title } = Typography;

interface ProductKnowledgePageProps {
  /** 占位 prop，为未来编辑能力预留；当前页面只读，始终为 false */
  onDirtyChange?: (dirty: boolean) => void;
}

interface HttpLikeError {
  response?: {
    status?: number;
    data?: {
      detail?: string;
    };
  };
  message?: string;
}

function getErrorMessage(error: unknown, fallback: string) {
  const maybeError = error as HttpLikeError;
  return maybeError.response?.data?.detail || maybeError.message || fallback;
}

function formatTime(iso: string): string {
  if (!iso) return '-';
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function ProductKnowledgePage({ onDirtyChange }: ProductKnowledgePageProps) {
  const [tree, setTree] = useState<KnowledgeTreeNode[]>([]);
  const [status, setStatus] = useState<KnowledgeStatus | null>(null);
  const [usageMap, setUsageMap] = useState<KnowledgeUsageItem[]>([]);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [selectedDoc, setSelectedDoc] = useState<KnowledgeDocument | null>(null);
  const [docLoading, setDocLoading] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 搜索状态
  const [searchKeyword, setSearchKeyword] = useState('');
  const [searchResults, setSearchResults] = useState<KnowledgeSearchResult[]>([]);
  const [searching, setSearching] = useState(false);

  // 只读页面，始终通知非脏状态
  useEffect(() => {
    onDirtyChange?.(false);
  }, [onDirtyChange]);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [treeData, statusData, usageData] = await Promise.all([
        knowledgeApi.getTree(true, true),
        knowledgeApi.getStatus(),
        knowledgeApi.getUsageMap(),
      ]);
      setTree(treeData.children || []);
      setStatus(statusData);
      setUsageMap(usageData);
    } catch (err) {
      setError(getErrorMessage(err, '加载知识库失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  const directScoringPaths = useMemo(() => {
    // 从搜索结果或目录树中无法直接获得该信息，留空集合。
    // 如需高亮，可在此扩展通过单独接口拉取。
    return new Set<string>();
  }, []);

  const handleSelect = useCallback(async (path: string) => {
    setSelectedPath(path);
    setDocLoading(true);
    setSelectedDoc(null);
    try {
      const doc = await knowledgeApi.getDocument(path);
      setSelectedDoc(doc);
    } catch (err) {
      message.error(getErrorMessage(err, '加载文档失败'));
    } finally {
      setDocLoading(false);
    }
  }, []);

  const handleSearch = useCallback(async (value: string) => {
    const keyword = value.trim();
    setSearchKeyword(keyword);
    if (!keyword) {
      setSearchResults([]);
      return;
    }
    setSearching(true);
    try {
      const results = await knowledgeApi.search(keyword);
      setSearchResults(results);
    } catch (err) {
      message.error(getErrorMessage(err, '搜索失败'));
      setSearchResults([]);
    } finally {
      setSearching(false);
    }
  }, []);

  const handleSearchResultClick = useCallback(
    (item: KnowledgeSearchResult) => {
      setSearchKeyword('');
      setSearchResults([]);
      void handleSelect(item.relative_path);
    },
    [handleSelect],
  );

  const renderLeftContent = () => {
    if (searchKeyword) {
      return (
        <Spin spinning={searching}>
          {searchResults.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="未找到匹配文件"
              style={{ padding: 24 }}
            />
          ) : (
            <List
              size="small"
              dataSource={searchResults}
              renderItem={(item) => (
                <List.Item
                  style={{ cursor: 'pointer', padding: '8px 4px' }}
                  onClick={() => handleSearchResultClick(item)}
                >
                  <Space direction="vertical" size={0} style={{ width: '100%' }}>
                    <Space>
                      <Text strong>{item.name}</Text>
                      <KnowledgeUsageBadge role={item.knowledge_role} />
                      {item.direct_scoring_prompt && (
                        <Tag color="gold" style={{ marginInlineStart: 0 }}>
                          核心打分
                        </Tag>
                      )}
                    </Space>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {item.relative_path}
                    </Text>
                    {item.snippet && (
                      <Text type="secondary" ellipsis style={{ fontSize: 12 }}>
                        {item.snippet}
                      </Text>
                    )}
                  </Space>
                </List.Item>
              )}
            />
          )}
        </Spin>
      );
    }
    return (
      <KnowledgeTree
        treeData={tree}
        onSelect={handleSelect}
        directScoringPaths={directScoringPaths}
        selectedPath={selectedPath || undefined}
      />
    );
  };

  return (
    <div style={{ padding: 24, background: '#f5f7fb', minHeight: 'calc(100vh - 64px)' }}>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Col>
          <Title level={4} style={{ margin: 0 }}>
            产品知识库
          </Title>
          <Paragraph type="secondary" style={{ margin: 0 }}>
            浏览产品知识库的正式文档和目录结构
          </Paragraph>
        </Col>
        <Col>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={loadData}>
            刷新
          </Button>
        </Col>
      </Row>

      {error && (
        <Alert
          type="error"
          showIcon
          closable
          message="知识库加载异常"
          description={error}
          onClose={() => setError(null)}
          style={{ marginBottom: 16 }}
        />
      )}

      {/* 状态卡片 */}
      <Card size="small" style={{ marginBottom: 16 }}>
        {loading ? (
          <Skeleton active paragraph={{ rows: 1 }} />
        ) : (
          <Row gutter={[24, 8]} align="middle">
            <Col>
              <Text type="secondary">知识哈希：</Text>
              <Text code>{status?.knowledge_hash?.slice(0, 8) || '-'}</Text>
            </Col>
            <Col>
              <Text type="secondary">加载时间：</Text>
              <Text>{status?.loaded_at ? formatTime(status.loaded_at) : '-'}</Text>
            </Col>
            <Col>
              <Text type="secondary">文件总数：</Text>
              <Text strong>{status?.file_count ?? 0}</Text>
            </Col>
            <Col>
              <Text type="secondary">评分相关：</Text>
              <Text strong>{status?.loader_relevant_count ?? 0}</Text>
            </Col>
            <Col>
              <Text type="secondary">核心打分：</Text>
              <Text strong>{status?.direct_scoring_file_count ?? 0}</Text>
            </Col>
            <Col>
              <Tag color={status?.loaded ? 'green' : 'default'}>
                {status?.loaded ? '已加载' : '未加载'}
              </Tag>
            </Col>
          </Row>
        )}
      </Card>

      {/* 用途图例 */}
      {usageMap.length > 0 && (
        <Card size="small" style={{ marginBottom: 16 }}>
          <Space wrap>
            {usageMap.map((item) => (
              <KnowledgeUsageBadge key={item.role} role={item.role} label={item.label} />
            ))}
          </Space>
        </Card>
      )}

      <Row gutter={16}>
        <Col xs={24} md={8}>
          <Card
            title="目录"
            size="small"
            styles={{ body: { padding: 12 } }}
          >
            <Input.Search
              placeholder="搜索文件名或内容"
              prefix={<SearchOutlined />}
              allowClear
              enterButton
              value={searchKeyword}
              onChange={(e) => {
                const val = e.target.value;
                if (!val) {
                  setSearchResults([]);
                }
                setSearchKeyword(val);
              }}
              onSearch={handleSearch}
              style={{ marginBottom: 12 }}
            />
            {loading ? (
              <Skeleton active paragraph={{ rows: 6 }} />
            ) : (
              renderLeftContent()
            )}
          </Card>
        </Col>
        <Col xs={24} md={16}>
          <Card title="文档内容" size="small" styles={{ body: { padding: 16 } }}>
            <KnowledgeViewer document={selectedDoc} loading={docLoading} />
          </Card>
        </Col>
      </Row>
    </div>
  );
}
