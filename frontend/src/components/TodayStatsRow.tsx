import { CalendarOutlined } from '@ant-design/icons';
import { Card, Col, Row, Skeleton, Space, Statistic, Typography } from 'antd';
import type { StatsData } from '../types';

const { Text } = Typography;

interface TodayStatsRowProps {
  stats: StatsData | null;
  loading: boolean;
}

const CARD_STYLES = [
  { background: '#f6ffed', borderColor: '#b7eb8f', color: '#389e0d' },
  { background: '#e6f7ff', borderColor: '#91d5ff', color: '#1677ff' },
  { background: '#fff7e6', borderColor: '#ffd591', color: '#d46b08' },
];

export default function TodayStatsRow({ stats, loading }: TodayStatsRowProps) {
  const date = new Date().toLocaleDateString('zh-CN');
  const items = [
    { title: '今日收录', value: stats?.today_count ?? 0, suffix: '篇文章' },
    { title: '今日 AI 安全', value: stats?.today_ai_security_count ?? 0, suffix: '篇相关' },
    { title: '今日高价值', value: stats?.today_high_value_count ?? 0, suffix: '篇(≥140分)' },
  ];

  return (
    <section aria-label="今日新增统计" style={{ marginBottom: 24 }}>
      <Space style={{ marginBottom: 12 }}>
        <CalendarOutlined />
        <Text strong>今日新增 ({date})</Text>
      </Space>
      <Row gutter={[16, 16]}>
        {items.map((item, index) => {
          const cardStyle = CARD_STYLES[index];
          return (
            <Col xs={24} sm={8} key={item.title}>
              <Card size="small" style={{ ...cardStyle, height: '100%' }}>
                {loading ? (
                  <Skeleton active paragraph={{ rows: 1 }} title={false} />
                ) : (
                  <Statistic
                    title={item.title}
                    value={item.value}
                    suffix={item.suffix}
                    valueStyle={{ color: cardStyle.color }}
                  />
                )}
              </Card>
            </Col>
          );
        })}
      </Row>
    </section>
  );
}
