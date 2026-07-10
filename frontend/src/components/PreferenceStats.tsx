import { BarChartOutlined } from '@ant-design/icons';
import { Card, Col, Empty, List, Progress, Row, Space, Typography } from 'antd';
import type { PreferenceMetric, PreferenceScores } from '../types';

const { Text } = Typography;

interface PreferenceStatsProps {
  scores?: PreferenceScores | null;
}

function metricWeight(metric: PreferenceMetric) {
  return metric.count + metric.download_count + metric.apply_count * 2 + metric.revise_count;
}

function metricPercent(metric: PreferenceMetric, maxWeight: number) {
  if (maxWeight <= 0) return 0;
  return Math.round((metricWeight(metric) / maxWeight) * 100);
}

function renderMetricList(title: string, data: Record<string, PreferenceMetric>) {
  const entries = Object.entries(data)
    .sort(([, left], [, right]) => metricWeight(right) - metricWeight(left))
    .slice(0, 6);
  const maxWeight = Math.max(...entries.map(([, metric]) => metricWeight(metric)), 0);

  return (
    <Card size="small" title={title} style={{ height: '100%' }}>
      {entries.length === 0 ? (
        <Empty description="暂无统计数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <List
          size="small"
          dataSource={entries}
          renderItem={([name, metric]) => (
            <List.Item>
              <Space direction="vertical" size={4} style={{ width: '100%' }}>
                <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                  <Text strong>{name}</Text>
                  <Text type="secondary">
                    {metric.count} 评 / {metric.avg_rating.toFixed(1)} 星
                  </Text>
                </Space>
                <Progress
                  percent={metricPercent(metric, maxWeight)}
                  size="small"
                  format={() =>
                    `下载 ${metric.download_count} · 应用 ${metric.apply_count} · 改稿 ${metric.revise_count}`
                  }
                />
              </Space>
            </List.Item>
          )}
        />
      )}
    </Card>
  );
}

export default function PreferenceStats({ scores }: PreferenceStatsProps) {
  return (
    <Card
      title={
        <Space>
          <BarChartOutlined />
          偏好统计
        </Space>
      }
      style={{ height: '100%' }}
    >
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={12}>
          {renderMetricList('模板偏好', scores?.template_scores ?? {})}
        </Col>
        <Col xs={24} lg={12}>
          {renderMetricList('视角偏好', scores?.perspective_scores ?? {})}
        </Col>
      </Row>
    </Card>
  );
}
