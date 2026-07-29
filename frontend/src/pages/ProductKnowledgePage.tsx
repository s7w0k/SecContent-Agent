/**
 * 产品知识库页面（K.3）
 *
 * 全员可见的只读浏览页面：左侧目录树 + 搜索，右侧文档查看器。
 * 管理员可创建草稿、编辑、校验、发布。
 */

import { EditOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Input,
  List,
  Modal,
  Row,
  Skeleton,
  Space,
  Spin,
  Tag,
  Typography,
  message,
} from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { knowledgeAdminApi, knowledgeApi } from '../api/client';
import { useAuth } from '../auth/useAuth';
import KnowledgeTree from '../components/KnowledgeTree';
import KnowledgeUsageBadge from '../components/KnowledgeUsageBadge';
import KnowledgeViewer from '../components/KnowledgeViewer';
import type {
  KnowledgeDocument,
  KnowledgeDraft,
  KnowledgeSearchResult,
  KnowledgeStatus,
  KnowledgeTreeNode,
  KnowledgeUsageItem,
} from '../types';

const { Paragraph, Text, Title } = Typography;

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

export default function ProductKnowledgePage() {
  const { user } = useAuth();
  const isAdmin = user?.is_admin ?? false;

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

  // 草稿编辑状态
  const [draftModalOpen, setDraftModalOpen] = useState(false);
  const [draftLoading, setDraftLoading] = useState(false);
  const [draftContent, setDraftContent] = useState('');
  const [currentDraft, setCurrentDraft] = useState<KnowledgeDraft | null>(null);
  const [draftSaving, setDraftSaving] = useState(false);

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

  // 创建草稿
  const handleCreateDraft = useCallback(async () => {
    if (!selectedDoc) return;
    if (!selectedDoc.editable) {
      message.warning('该文件不允许编辑');
      return;
    }
    setDraftLoading(true);
    try {
      const draft = await knowledgeAdminApi.createDraft(
        selectedDoc.document_id,
        selectedDoc.content_hash,
      );
      setCurrentDraft(draft);
      setDraftContent(draft.content_md);
      setDraftModalOpen(true);
    } catch (err) {
      message.error(getErrorMessage(err, '创建草稿失败'));
    } finally {
      setDraftLoading(false);
    }
  }, [selectedDoc]);

  // 保存草稿
  const handleSaveDraft = useCallback(async () => {
    if (!currentDraft) return;
    if (!draftContent.trim()) {
      message.warning('草稿内容不能为空');
      return;
    }
    setDraftSaving(true);
    try {
      const updated = await knowledgeAdminApi.updateDraft(
        currentDraft.draft_id,
        draftContent,
      );
      setCurrentDraft(updated);
      message.success('草稿已保存');
    } catch (err) {
      message.error(getErrorMessage(err, '保存失败'));
    } finally {
      setDraftSaving(false);
    }
  }, [currentDraft, draftContent]);

  // 校验草稿
  const handleValidateDraft = useCallback(async () => {
    if (!currentDraft) return;
    setDraftSaving(true);
    try {
      const result = await knowledgeAdminApi.validateDraft(currentDraft.draft_id);
      if (result.status === 'passed') {
        message.success(`校验通过（${result.loader_file_count} 个文件，${result.loader_relevant_count} 个评分相关）`);
      } else {
        message.warning(`校验失败：${result.errors.join('; ')}`);
      }
      // 刷新草稿状态
      const data = await knowledgeAdminApi.getDraft(currentDraft.draft_id);
      setCurrentDraft(data.draft);
    } catch (err) {
      message.error(getErrorMessage(err, '校验失败'));
    } finally {
      setDraftSaving(false);
    }
  }, [currentDraft]);

  // 发布草稿
  const handlePublishDraft = useCallback(async () => {
    if (!currentDraft) return;
    Modal.confirm({
      title: '发布到正式知识库',
      content: '发布后将立即生效，影响后续文章打分和草稿生成。确认发布？',
      okText: '发布',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        setDraftSaving(true);
        try {
          await knowledgeAdminApi.publish([currentDraft.draft_id]);
          message.success('发布成功，知识库已刷新');
          setDraftModalOpen(false);
          setCurrentDraft(null);
          await loadData();
        } catch (err) {
          message.error(getErrorMessage(err, '发布失败'));
        } finally {
          setDraftSaving(false);
        }
      },
    });
  }, [currentDraft, loadData]);

  // 放弃草稿
  const handleDiscardDraft = useCallback(async () => {
    if (!currentDraft) return;
    Modal.confirm({
      title: '放弃草稿',
      content: '放弃后草稿将被删除，不影响正式文件。',
      okText: '放弃',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        setDraftSaving(true);
        try {
          await knowledgeAdminApi.deleteDraft(currentDraft.draft_id);
          message.success('草稿已放弃');
          setDraftModalOpen(false);
          setCurrentDraft(null);
        } catch (err) {
          message.error(getErrorMessage(err, '放弃失败'));
        } finally {
          setDraftSaving(false);
        }
      },
    });
  }, [currentDraft]);

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
          <Space>
            {isAdmin && selectedDoc?.editable && (
              <Button
                type="primary"
                icon={<EditOutlined />}
                loading={draftLoading}
                onClick={handleCreateDraft}
              >
                编辑
              </Button>
            )}
            <Button icon={<ReloadOutlined />} loading={loading} onClick={loadData}>
              刷新
            </Button>
          </Space>
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
          <Card
            title="文档内容"
            size="small"
            styles={{ body: { padding: 16 } }}
          >
            <KnowledgeViewer document={selectedDoc} loading={docLoading} />
          </Card>
        </Col>
      </Row>

      {/* 草稿编辑弹窗 */}
      <Modal
        title={`编辑草稿 - ${currentDraft?.relative_path ?? ''}`}
        open={draftModalOpen}
        onCancel={() => {
          setDraftModalOpen(false);
          setCurrentDraft(null);
        }}
        width={900}
        footer={
          <Space>
            <Button
              danger
              onClick={handleDiscardDraft}
              loading={draftSaving}
            >
              放弃草稿
            </Button>
            <Button onClick={handleValidateDraft} loading={draftSaving}>
              校验
            </Button>
            <Button onClick={handleSaveDraft} loading={draftSaving}>
              保存
            </Button>
            <Button type="primary" onClick={handlePublishDraft} loading={draftSaving}>
              发布
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {currentDraft?.validation && (
            <Alert
              type={currentDraft.validation.status === 'passed' ? 'success' : 'error'}
              showIcon
              message={
                currentDraft.validation.status === 'passed'
                  ? '校验通过'
                  : `校验失败：${currentDraft.validation.errors.join('; ')}`
              }
              description={
                currentDraft.validation.warnings.length > 0
                  ? currentDraft.validation.warnings.join('\n')
                  : undefined
              }
            />
          )}
          <Input.TextArea
            value={draftContent}
            onChange={(e) => setDraftContent(e.target.value)}
            rows={20}
            style={{ fontFamily: 'monospace', fontSize: 13 }}
          />
          <Text type="secondary" style={{ fontSize: 12 }}>
            草稿 ID: {currentDraft?.draft_id ?? '-'} · 状态: {currentDraft?.status ?? '-'} ·
            更新时间: {currentDraft?.updated_at ? formatTime(currentDraft.updated_at) : '-'}
          </Text>
        </Space>
      </Modal>
    </div>
  );
}
