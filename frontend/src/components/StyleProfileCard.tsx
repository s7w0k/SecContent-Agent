import { EditOutlined, FileTextOutlined, MessageOutlined, TagsOutlined } from '@ant-design/icons';
import { Card, Descriptions, Empty, Space, Tag, Typography } from 'antd';
import type { StyleProfile } from '../types';

const { Paragraph, Text } = Typography;

const LENGTH_LABELS = {
  short: '短篇幅',
  medium: '中等篇幅',
  long: '长篇幅',
};

const TONE_LABELS = {
  market_oriented: '市场传播向',
  technical: '技术深度向',
  executive: '高管决策向',
};

interface StyleProfileCardProps {
  profile: StyleProfile | null;
}

function renderTags(values: string[], emptyText: string) {
  if (!values.length) {
    return <Text type="secondary">{emptyText}</Text>;
  }
  return (
    <Space size={[0, 6]} wrap>
      {values.map((value) => (
        <Tag key={value} color="blue">
          {value}
        </Tag>
      ))}
    </Space>
  );
}

export default function StyleProfileCard({ profile }: StyleProfileCardProps) {
  if (!profile) {
    return (
      <Card title="风格画像" style={{ height: '100%' }}>
        <Empty description="暂无画像数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      </Card>
    );
  }

  const hints = profile.style_hints;

  return (
    <Card
      title={
        <Space>
          <TagsOutlined />
          风格画像
        </Space>
      }
      style={{ height: '100%' }}
    >
      <Descriptions column={1} size="small">
        <Descriptions.Item
          label={
            <>
              <FileTextOutlined /> 偏好模板
            </>
          }
        >
          {renderTags(hints.preferred_templates, '暂无模板偏好')}
        </Descriptions.Item>
        <Descriptions.Item
          label={
            <>
              <MessageOutlined /> 偏好视角
            </>
          }
        >
          {renderTags(hints.preferred_perspectives, '暂无视角偏好')}
        </Descriptions.Item>
        <Descriptions.Item label="偏好篇幅">
          <Tag color="purple">{LENGTH_LABELS[hints.preferred_length]}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="偏好语气">
          <Tag color="magenta">{TONE_LABELS[hints.preferred_tone]}</Tag>
        </Descriptions.Item>
        <Descriptions.Item
          label={
            <>
              <EditOutlined /> 常见改稿
            </>
          }
        >
          {renderTags(hints.common_revise_directions, '暂无稳定改稿方向')}
        </Descriptions.Item>
        <Descriptions.Item label="规避模式">
          {renderTags(hints.avoid_patterns, '暂无规避模式')}
        </Descriptions.Item>
      </Descriptions>

      {profile.llm_analysis && (
        <Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
          {profile.llm_analysis}
        </Paragraph>
      )}
    </Card>
  );
}
