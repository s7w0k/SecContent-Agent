import { DashboardOutlined, ReloadOutlined } from '@ant-design/icons';
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Row,
  Skeleton,
  Space,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { memoryApi, policyApi, profileApi } from '../api/client';
import { useAuth } from '../auth/useAuth';
import MemoryEvidenceDrawer from '../components/MemoryEvidenceDrawer';
import MemoryItemCard from '../components/MemoryItemCard';
import ProfilePolicyEditor from '../components/ProfilePolicyEditor';
import StyleProfileCard from '../components/StyleProfileCard';
import type { MemoryItem, ProfilePolicy, StyleProfile } from '../types';

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

function isNotFound(error: unknown) {
  return (error as HttpLikeError).response?.status === 404;
}

// ── 偏好概览摘要 ──────────────────────────────────

const CONTENT_FOCUS_LABELS: Record<string, string> = {
  product_tech: '产品技术亮点',
  industry_trend: '行业趋势洞察',
  customer_value: '客户案例价值',
  brand_authority: '品牌权威定位',
  solution_advantage: '解决方案优势',
};

const STRUCTURE_LABELS: Record<string, string> = {
  inverted_pyramid: '倒金字塔',
  problem_solution: '问题-方案-总结',
  storytelling: '故事线',
  progressive: '递进式',
};

function summarizePolicy(policy: ProfilePolicy | null): string {
  if (!policy) return '尚未配置显式偏好';
  const parts: string[] = [];
  if (policy.content_focus?.length) {
    const labels = policy.content_focus.map((v) => CONTENT_FOCUS_LABELS[v] || v).join('、');
    parts.push(`内容侧重「${labels}」`);
  }
  if (policy.structure_preference) parts.push(`结构「${STRUCTURE_LABELS[policy.structure_preference] || policy.structure_preference}」`);
  if (policy.required_patterns?.length) parts.push(`${policy.required_patterns.length} 项必含要素`);
  if (policy.avoid_patterns?.length) parts.push(`${policy.avoid_patterns.length} 项规避要素`);
  if (policy.custom_instructions) parts.push('含自定义说明');
  return parts.length ? parts.join('，') : '尚未配置显式偏好';
}

function summarizeMemory(stats: Record<string, number> | undefined, pending: number): string {
  const total = stats ? Object.values(stats).reduce((a, b) => a + b, 0) : 0;
  if (total === 0) return '暂无自动学习记忆';
  const active = stats?.active ?? 0;
  if (pending > 0) return `已学习 ${total} 条记忆，其中 ${active} 条已生效、${pending} 条待审批`;
  return `已学习 ${total} 条记忆，全部已生效`;
}

function ProfileEmptyState({
  onRebuild,
  rebuilding,
}: { onRebuild: () => void; rebuilding: boolean }) {
  return (
    <Card>
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={
          <Space direction="vertical">
            <Text strong>用户画像尚未生成</Text>
            <Text type="secondary">
              开始提交草稿反馈、下载草稿或应用修订后，系统会积累偏好信号；也可以手动重建画像。
            </Text>
          </Space>
        }
      >
        <Button type="primary" icon={<ReloadOutlined />} onClick={onRebuild} loading={rebuilding}>
          重建画像
        </Button>
      </Empty>
    </Card>
  );
}

export default function ProfilePage() {
  const { user } = useAuth();
  const [profile, setProfile] = useState<StyleProfile | null>(null);
  const [policy, setPolicy] = useState<ProfilePolicy | null>(null);
  const [loading, setLoading] = useState(true);
  const [rebuilding, setRebuilding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // ── 记忆审计状态 ──────────────────────────────────
  const [memoryItems, setMemoryItems] = useState<MemoryItem[]>([]);
  const [memoryLoading, setMemoryLoading] = useState(false);
  const [memoryStatusFilter, setMemoryStatusFilter] = useState<string>('active,pending_approval');
  const [memoryStats, setMemoryStats] = useState<Record<string, number>>({});
  const [memoryPending, setMemoryPending] = useState(0);
  const [evidenceItem, setEvidenceItem] = useState<MemoryItem | null>(null);

  const loadProfile = useCallback(async () => {
    try {
      const nextProfile = await profileApi.getStyle();
      setProfile(nextProfile);
    } catch (err) {
      if (isNotFound(err)) {
        setProfile(null);
        return;
      }
      throw err;
    }
  }, []);

  const loadPolicy = useCallback(async () => {
    try {
      const res = await policyApi.getPolicy();
      setPolicy(res.data.policy);
    } catch {
      setPolicy(null);
    }
  }, []);

  const loadMemoryItems = useCallback(async () => {
    setMemoryLoading(true);
    try {
      const res = await memoryApi.listItems({ status: memoryStatusFilter, page: 1, page_size: 50 });
      setMemoryItems(res.data.items);
      setMemoryStats(res.data.status_stats ?? {});
      setMemoryPending(res.data.pending_count ?? 0);
    } catch {
      setMemoryItems([]);
      setMemoryStats({});
      setMemoryPending(0);
    } finally {
      setMemoryLoading(false);
    }
  }, [memoryStatusFilter]);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      await Promise.all([loadProfile(), loadPolicy(), loadMemoryItems()]);
    } catch (err) {
      setError(getErrorMessage(err, '加载用户画像失败'));
    } finally {
      setLoading(false);
    }
  }, [loadProfile, loadPolicy, loadMemoryItems]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const handleRebuild = async () => {
    setRebuilding(true);
    setError(null);
    try {
      const result = await profileApi.rebuild();
      message.success(`画像已重建，版本 v${result.version}`);
      await loadData();
    } catch (err) {
      setError(getErrorMessage(err, '重建画像失败'));
    } finally {
      setRebuilding(false);
    }
  };

  const handleMemoryAction = useCallback(async (action: string, memoryId: string) => {
    try {
      if (action === 'approve') await memoryApi.approveItem(memoryId);
      else if (action === 'reject') await memoryApi.rejectItem(memoryId);
      else if (action === 'suppress') await memoryApi.suppressItem(memoryId);
      else if (action === 'activate') await memoryApi.activateItem(memoryId);
      else if (action === 'delete') await memoryApi.deleteItem(memoryId);
      message.success('操作成功');
      await loadMemoryItems();
    } catch {
      message.error('操作失败');
    }
  }, [loadMemoryItems]);

  const lastUpdated = useMemo(() => {
    if (!profile?.updated_at) return '尚未生成';
    return new Date(profile.updated_at).toLocaleString();
  }, [profile?.updated_at]);

  return (
    <div style={{ padding: 24, background: '#f5f7fb', minHeight: 'calc(100vh - 64px)' }}>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Col>
          <Space direction="vertical" size={0}>
            <Title level={3} style={{ margin: 0 }}>
              {user?.display_name || user?.username} 的用户画像
            </Title>
            <Paragraph type="secondary" style={{ margin: 0 }}>
              基于反馈、下载、改稿与应用记录学习你的 PR 草稿偏好。
            </Paragraph>
          </Space>
        </Col>
        <Col>
          <Space>
            <Tag icon={<DashboardOutlined />} color={profile ? 'blue' : 'default'}>
              {profile ? `v${profile.version} · ${lastUpdated}` : '暂无画像'}
            </Tag>
            <Button icon={<ReloadOutlined />} onClick={loadData} loading={loading}>
              刷新
            </Button>
            <Button
              type="primary"
              icon={<ReloadOutlined />}
              onClick={handleRebuild}
              loading={rebuilding}
            >
              重建画像
            </Button>
          </Space>
        </Col>
      </Row>

      {error && (
        <Alert
          type="error"
          showIcon
          closable
          message="用户画像加载异常"
          description={error}
          onClose={() => setError(null)}
          style={{ marginBottom: 16 }}
        />
      )}

      {loading ? (
        <div data-testid="profile-loading">
          <Skeleton active paragraph={{ rows: 10 }} />
        </div>
      ) : (
        <Tabs
          defaultActiveKey="overview"
          items={[
            {
              key: 'overview',
              label: '画像概览',
              children: !profile ? (
                <ProfileEmptyState onRebuild={handleRebuild} rebuilding={rebuilding} />
              ) : (
                <Row gutter={[16, 16]}>
                  <Col xs={24} lg={12}>
                    <StyleProfileCard profile={profile} />
                  </Col>
                  <Col xs={24} lg={12}>
                    <Card title="偏好概览" style={{ height: '100%' }}>
                      <Space direction="vertical" size="large" style={{ width: '100%' }}>
                        <div>
                          <Text type="secondary" style={{ fontSize: 13 }}>显式偏好</Text>
                          <Paragraph style={{ margin: '4px 0 0' }}>
                            {summarizePolicy(policy)}
                          </Paragraph>
                        </div>
                        <div>
                          <Text type="secondary" style={{ fontSize: 13 }}>自动记忆</Text>
                          <Paragraph style={{ margin: '4px 0 0' }}>
                            {summarizeMemory(memoryStats, memoryPending)}
                          </Paragraph>
                        </div>
                      </Space>
                    </Card>
                  </Col>
                </Row>
              ),
            },
            {
              key: 'policy',
              label: '显式偏好',
              children: <ProfilePolicyEditor />,
            },
            {
              key: 'memory',
              label: '自动记忆',
              children: (
                <Card
                  title="自动学习记忆"
                  extra={
                    <Space>
                      <Tag
                        color={memoryStatusFilter.includes('pending') ? 'orange' : 'blue'}
                        style={{ cursor: 'pointer' }}
                        onClick={() => {
                          const next = memoryStatusFilter.includes('pending')
                            ? 'active'
                            : 'active,pending_approval';
                          setMemoryStatusFilter(next);
                        }}
                      >
                        {memoryStatusFilter.includes('pending') ? '含待审批' : '仅生效中'}
                      </Tag>
                      <Button
                        size="small"
                        icon={<ReloadOutlined />}
                        onClick={loadMemoryItems}
                        loading={memoryLoading}
                      >
                        刷新
                      </Button>
                    </Space>
                  }
                >
                  {memoryLoading ? (
                    <Skeleton active paragraph={{ rows: 5 }} />
                  ) : memoryItems.length === 0 ? (
                    <Empty description="暂无自动学习记忆" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                  ) : (
                    <Row gutter={[12, 12]}>
                      {memoryItems.map((item) => (
                        <Col xs={24} md={12} key={item.memory_id}>
                          <MemoryItemCard
                            item={item}
                            onAction={(action) => {
                              if (action === 'evidence') {
                                setEvidenceItem(item);
                              } else {
                                handleMemoryAction(action, item.memory_id);
                              }
                            }}
                          />
                        </Col>
                      ))}
                    </Row>
                  )}
                </Card>
              ),
            },
          ]}
          onChange={(key) => {
            if (key === 'memory' && memoryItems.length === 0) {
              loadMemoryItems();
            }
          }}
        />
      )}

      <MemoryEvidenceDrawer
        open={!!evidenceItem}
        item={evidenceItem}
        onClose={() => setEvidenceItem(null)}
      />
    </div>
  );
}
