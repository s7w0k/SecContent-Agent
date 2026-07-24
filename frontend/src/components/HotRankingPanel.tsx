import { FireOutlined, SyncOutlined } from '@ant-design/icons';
import { Alert, Button, Card, Empty, List, Radio, Select, Space, Spin, Tag, Typography } from 'antd';
import { useCallback, useEffect, useRef, useState } from 'react';
import api from '../api/client';
import type { HotArticle, HotRankingQuery } from '../types';

const { Text } = Typography;
type DateRange = NonNullable<HotRankingQuery['date_range']>;

const CATEGORY_OPTIONS = [
  { value: 'all', label: '全部分类' },
  { value: '爆点事件', label: '爆点事件' },
  { value: '法律法规/监管动态', label: '法律法规/监管动态' },
  { value: 'AI技术重大进展', label: 'AI技术重大进展' },
  { value: '国内外竞品信息', label: '国内外竞品信息' },
  { value: '运营商/行业事件', label: '运营商/行业事件' },
  { value: '学术/会展/高校', label: '学术/会展/高校' },
];

const DATE_RANGE_OPTIONS: Array<{ label: string; value: DateRange }> = [
  { label: '今日', value: '1d' },
  { label: '近7天', value: '7d' },
  { label: '近30天', value: '30d' },
  { label: '全部', value: 'all' },
];

const MEDALS = ['🥇', '🥈', '🥉'];

export default function HotRankingPanel() {
  const [category, setCategory] = useState('all');
  const [dateRange, setDateRange] = useState<DateRange>('7d');
  const [items, setItems] = useState<HotArticle[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const requestId = useRef(0);

  const loadRanking = useCallback(async () => {
    const currentRequest = ++requestId.current;
    setLoading(true);
    setError(null);
    try {
      const result = await api.getHotRanking({ limit: 10, category, date_range: dateRange });
      if (currentRequest === requestId.current) {
        setItems(result);
        setError(null);
      }
    } catch {
      if (currentRequest === requestId.current) {
        setError('热点排行加载失败，请检查网络或稍后重试');
      }
    } finally {
      if (currentRequest === requestId.current) setLoading(false);
    }
  }, [category, dateRange]);

  useEffect(() => {
    void loadRanking();
  }, [loadRanking]);

  return (
    <Card
      title={
        <Space>
          <FireOutlined style={{ color: '#ff4d4f' }} />
          热点排行
        </Space>
      }
      extra={
        <Button
          type="text"
          size="small"
          aria-label="刷新热点排行"
          icon={<SyncOutlined spin={loading} />}
          onClick={() => void loadRanking()}
        >
          刷新
        </Button>
      }
      style={{ height: '100%' }}
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Select
          aria-label="热点分类"
          value={category}
          options={CATEGORY_OPTIONS}
          onChange={setCategory}
          style={{ width: '100%' }}
        />
        <Radio.Group
          aria-label="热点时间范围"
          value={dateRange}
          options={DATE_RANGE_OPTIONS}
          onChange={(event) => setDateRange(event.target.value as DateRange)}
          optionType="button"
          buttonStyle="solid"
          size="small"
        />

        <Spin spinning={loading}>
          {!loading && error ? (
            <Alert
              type="error"
              showIcon
              message="热点排行加载失败"
              description={error}
              action={
                <Button
                  size="small"
                  danger
                  aria-label="重试热点排行"
                  onClick={() => void loadRanking()}
                >
                  重试
                </Button>
              }
            />
          ) : !loading && items.length === 0 ? (
            <Empty
              description="暂无高价值文章，可尝试扩大时间范围或分类"
              image={Empty.PRESENTED_IMAGE_SIMPLE}
            />
          ) : (
            <List
              size="small"
              dataSource={items}
              renderItem={(article, index) => (
                <List.Item>
                  <Space align="center" style={{ width: '100%' }} wrap>
                    <Text
                      aria-label={`第 ${index + 1} 名`}
                      style={{ minWidth: 24, textAlign: 'center' }}
                    >
                      {MEDALS[index] ?? index + 1}
                    </Text>
                    <a href={article.url} target="_blank" rel="noopener noreferrer">
                      {article.title}
                    </a>
                    <Tag color="blue">{article.pr_total_score} 分</Tag>
                    <Tag>{article.category_v2 || '未分类'}</Tag>
                  </Space>
                </List.Item>
              )}
            />
          )}
        </Spin>
      </Space>
    </Card>
  );
}
