import { BarChartOutlined, DownOutlined, UpOutlined } from '@ant-design/icons';
import { Button, Collapse, Empty, Progress, Skeleton, Space, Typography } from 'antd';
import { useMemo, useState } from 'react';

const { Text } = Typography;
const COLLAPSE_THRESHOLD = 8;
const DEFAULT_VISIBLE_COUNT = 6;

interface CategoryBreakdownProps {
  distribution: Record<string, number>;
  loading: boolean;
  onCategoryClick?: (category: string) => void;
}

export default function CategoryBreakdown({
  distribution,
  loading,
  onCategoryClick,
}: CategoryBreakdownProps) {
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);

  const entries = useMemo(
    () => Object.entries(distribution).sort(([, left], [, right]) => right - left),
    [distribution],
  );
  const total = entries.reduce((sum, [, count]) => sum + count, 0);
  const maxCount = entries[0]?.[1] ?? 0;
  const canToggle = entries.length > COLLAPSE_THRESHOLD;
  const visibleEntries = canToggle && !showAll ? entries.slice(0, DEFAULT_VISIBLE_COUNT) : entries;

  const selectCategory = (category: string) => {
    setSelectedCategory(category);
    onCategoryClick?.(category);
  };

  const content = loading ? (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      {[1, 2, 3, 4].map((item) => (
        <Skeleton key={item} active title={false} paragraph={{ rows: 1 }} />
      ))}
    </Space>
  ) : entries.length === 0 ? (
    <Empty description="暂无分类数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />
  ) : (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      {visibleEntries.map(([category, count]) => {
        const share = total > 0 ? (count / total) * 100 : 0;
        const barPercent = maxCount > 0 ? (count / maxCount) * 100 : 0;
        const selected = selectedCategory === category;

        return (
          <button
            key={category}
            type="button"
            aria-pressed={selected}
            onClick={() => selectCategory(category)}
            style={{
              width: '100%',
              padding: '8px 12px',
              border: selected ? '1px solid #91caff' : '1px solid transparent',
              borderRadius: 8,
              background: selected ? '#e6f4ff' : 'transparent',
              cursor: 'pointer',
              textAlign: 'left',
            }}
          >
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                <Text strong={selected}>{category}</Text>
                <Text type="secondary">
                  {count} ({share.toFixed(1)}%)
                </Text>
              </Space>
              <Progress percent={barPercent} showInfo={false} size="small" />
            </Space>
          </button>
        );
      })}
      {canToggle && (
        <Button
          type="link"
          size="small"
          icon={showAll ? <UpOutlined /> : <DownOutlined />}
          onClick={() => setShowAll((current) => !current)}
        >
          {showAll ? '收起' : '展开全部'}
        </Button>
      )}
    </Space>
  );

  return (
    <Collapse
      defaultActiveKey={['category-breakdown']}
      items={[
        {
          key: 'category-breakdown',
          label: (
            <Space>
              <BarChartOutlined />
              <Text strong>分类分布</Text>
            </Space>
          ),
          children: content,
        },
      ]}
    />
  );
}
