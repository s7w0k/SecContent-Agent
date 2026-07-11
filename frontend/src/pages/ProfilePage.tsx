import {
  DashboardOutlined,
  DownloadOutlined,
  EditOutlined,
  ReloadOutlined,
  StarOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  List,
  Progress,
  Row,
  Skeleton,
  Space,
  Statistic,
  Tag,
  Typography,
  message,
} from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { activityApi, profileApi } from '../api/client';
import { useAuth } from '../auth/useAuth';
import ActivityTimeline from '../components/ActivityTimeline';
import PreferenceStats from '../components/PreferenceStats';
import StyleProfileCard from '../components/StyleProfileCard';
import type { ActivityStats, StyleProfile, UserActivity } from '../types';

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

function renderTags(tags: string[]) {
  if (!tags.length) return <Text type="secondary">暂无高频标签</Text>;
  return (
    <Space size={[0, 6]} wrap>
      {tags.map((tag) => (
        <Tag key={tag} color="blue">
          {tag}
        </Tag>
      ))}
    </Space>
  );
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

function FeedbackSummaryCard({ profile }: { profile: StyleProfile | null }) {
  const summary = profile?.feedback_summary;
  return (
    <Card title="反馈汇总" style={{ height: '100%' }}>
      <Row gutter={[16, 16]}>
        <Col span={12}>
          <Statistic title="总反馈" value={summary?.total_feedbacks ?? 0} />
        </Col>
        <Col span={12}>
          <Statistic title="平均评分" value={summary?.avg_rating ?? 0} precision={1} suffix="星" />
        </Col>
        <Col span={8}>
          <Statistic title="正面" value={summary?.positive_count ?? 0} />
        </Col>
        <Col span={8}>
          <Statistic title="中性" value={summary?.neutral_count ?? 0} />
        </Col>
        <Col span={8}>
          <Statistic title="负面" value={summary?.negative_count ?? 0} />
        </Col>
      </Row>
      <div style={{ marginTop: 16 }}>
        <Text type="secondary">Top 标签</Text>
        <div style={{ marginTop: 8 }}>{renderTags(summary?.top_tags ?? [])}</div>
      </div>
    </Card>
  );
}

function ActivityStatsCard({
  profile,
  stats,
}: {
  profile: StyleProfile | null;
  stats: ActivityStats | null;
}) {
  const dailyTrend = stats?.daily_trend ?? [];
  const maxDaily = Math.max(...dailyTrend.map((item) => item.count), 0);

  return (
    <Card title="操作统计" style={{ height: '100%' }}>
      <Row gutter={[16, 16]}>
        <Col span={8}>
          <Statistic
            title="下载"
            value={
              profile?.activity_summary.total_downloads ?? stats?.by_action.draft_download ?? 0
            }
            prefix={<DownloadOutlined />}
          />
        </Col>
        <Col span={8}>
          <Statistic
            title="应用"
            value={profile?.activity_summary.total_applies ?? stats?.by_action.revision_apply ?? 0}
            prefix={<StarOutlined />}
          />
        </Col>
        <Col span={8}>
          <Statistic
            title="改稿"
            value={profile?.activity_summary.total_revises ?? stats?.by_action.draft_revise ?? 0}
            prefix={<EditOutlined />}
          />
        </Col>
      </Row>

      <div style={{ marginTop: 16 }}>
        <Text type="secondary">近 30 天趋势</Text>
        {dailyTrend.length === 0 ? (
          <Empty description="暂无趋势数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <List
            size="small"
            dataSource={dailyTrend.slice(-7)}
            renderItem={(item) => (
              <List.Item>
                <Space direction="vertical" size={4} style={{ width: '100%' }}>
                  <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                    <Text>{item.date}</Text>
                    <Text type="secondary">{item.count} 次</Text>
                  </Space>
                  <Progress
                    percent={maxDaily > 0 ? Math.round((item.count / maxDaily) * 100) : 0}
                    size="small"
                    showInfo={false}
                  />
                </Space>
              </List.Item>
            )}
          />
        )}
      </div>
    </Card>
  );
}

export default function ProfilePage() {
  const { user } = useAuth();
  const [profile, setProfile] = useState<StyleProfile | null>(null);
  const [activities, setActivities] = useState<UserActivity[]>([]);
  const [activityStats, setActivityStats] = useState<ActivityStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [activitiesLoading, setActivitiesLoading] = useState(false);
  const [rebuilding, setRebuilding] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  const loadActivities = useCallback(async () => {
    setActivitiesLoading(true);
    try {
      const [list, stats] = await Promise.all([
        activityApi.list({ page: 1, page_size: 20 }),
        activityApi.stats(30),
      ]);
      setActivities(list.items);
      setActivityStats(stats);
    } finally {
      setActivitiesLoading(false);
    }
  }, []);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      await Promise.all([loadProfile(), loadActivities()]);
    } catch (err) {
      setError(getErrorMessage(err, '加载用户画像失败'));
    } finally {
      setLoading(false);
    }
  }, [loadActivities, loadProfile]);

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
      ) : !profile ? (
        <ProfileEmptyState onRebuild={handleRebuild} rebuilding={rebuilding} />
      ) : (
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Row gutter={[16, 16]}>
            <Col xs={24} lg={10}>
              <StyleProfileCard profile={profile} />
            </Col>
            <Col xs={24} lg={14}>
              <PreferenceStats scores={profile.preference_scores} />
            </Col>
          </Row>

          <Row gutter={[16, 16]}>
            <Col xs={24} lg={12}>
              <FeedbackSummaryCard profile={profile} />
            </Col>
            <Col xs={24} lg={12}>
              <ActivityStatsCard profile={profile} stats={activityStats} />
            </Col>
          </Row>

          <ActivityTimeline activities={activities} loading={activitiesLoading} />
        </Space>
      )}
    </div>
  );
}
