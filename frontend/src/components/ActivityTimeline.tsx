import {
  CheckCircleOutlined,
  CloudDownloadOutlined,
  EditOutlined,
  FormOutlined,
  HistoryOutlined,
  StarOutlined,
} from '@ant-design/icons';
import { Card, Empty, Space, Spin, Tag, Timeline, Typography } from 'antd';
import dayjs from 'dayjs';
import type { ActionType, UserActivity } from '../types';

const { Text } = Typography;

const ACTION_LABELS: Record<ActionType, string> = {
  draft_view: '查看草稿',
  draft_download: '下载草稿',
  draft_revise: '发起改稿',
  revision_apply: '应用修订',
  feedback_submit: '提交反馈',
  pipeline_run: '触发流水线',
};

const ACTION_COLORS: Record<ActionType, string> = {
  draft_view: 'blue',
  draft_download: 'green',
  draft_revise: 'purple',
  revision_apply: 'cyan',
  feedback_submit: 'gold',
  pipeline_run: 'red',
};

const ACTION_ICONS: Record<ActionType, React.ReactNode> = {
  draft_view: <FormOutlined />,
  draft_download: <CloudDownloadOutlined />,
  draft_revise: <EditOutlined />,
  revision_apply: <CheckCircleOutlined />,
  feedback_submit: <StarOutlined />,
  pipeline_run: <HistoryOutlined />,
};

interface ActivityTimelineProps {
  activities: UserActivity[];
  loading?: boolean;
}

function formatActivity(activity: UserActivity) {
  const title =
    typeof activity.context.article_title === 'string'
      ? activity.context.article_title
      : activity.target.article_url_hash;
  const template = activity.target.template;
  const perspective = activity.target.perspective;

  return (
    <Space direction="vertical" size={4}>
      <Space size={[4, 4]} wrap>
        <Tag color={ACTION_COLORS[activity.action]}>{ACTION_LABELS[activity.action]}</Tag>
        {template && <Tag>{template}</Tag>}
        {perspective && <Tag color="blue">{perspective}</Tag>}
        {activity.target.revision_id && <Tag color="purple">修订稿</Tag>}
      </Space>
      <Text>{title || '未关联文章'}</Text>
      <Text type="secondary" style={{ fontSize: 12 }}>
        {dayjs(activity.created_at).format('YYYY-MM-DD HH:mm:ss')}
      </Text>
    </Space>
  );
}

export default function ActivityTimeline({ activities, loading = false }: ActivityTimelineProps) {
  return (
    <Card
      title={
        <Space>
          <HistoryOutlined />
          操作记录时间线
        </Space>
      }
    >
      <Spin spinning={loading}>
        {activities.length === 0 ? (
          <Empty description="暂无操作记录" />
        ) : (
          <Timeline
            items={activities.map((activity) => ({
              color: ACTION_COLORS[activity.action],
              dot: ACTION_ICONS[activity.action],
              children: formatActivity(activity),
            }))}
          />
        )}
      </Spin>
    </Card>
  );
}
