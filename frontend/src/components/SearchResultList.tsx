import { Card, Checkbox, Empty, Space, Tag, Typography } from 'antd';
import { LinkOutlined } from '@ant-design/icons';
import type { WebSearchResult } from '../types';

const { Text, Paragraph } = Typography;

interface SearchResultListProps {
  results: WebSearchResult[];
  selectedIds: Set<string>;
  onToggle: (resultId: string) => void;
  maxSelection: number;
}

export default function SearchResultList({ results, selectedIds, onToggle, maxSelection }: SearchResultListProps) {
  if (!results.length) {
    return <Empty description="暂无搜索结果" />;
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="small">
      {results.map((result) => {
        const isSelected = selectedIds.has(result.result_id);
        const isDisabled = result.is_imported || (!isSelected && selectedIds.size >= maxSelection);

        return (
          <Card
            key={result.result_id}
            size="small"
            styles={{ body: { padding: '12px 16px' } }}
          >
            <Space align="start" style={{ width: '100%' }}>
              <Checkbox
                checked={isSelected}
                disabled={isDisabled}
                onChange={() => onToggle(result.result_id)}
                style={{ marginTop: 4 }}
              />
              <div style={{ flex: 1, minWidth: 0 }}>
                <Space size="small" wrap>
                  <Text strong ellipsis style={{ maxWidth: 500 }}>
                    {result.title}
                  </Text>
                  {result.is_imported && <Tag color="default">已入库</Tag>}
                </Space>
                <div>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {result.display_domain}
                    {result.published_at && ` · ${result.published_at.split('T')[0]}`}
                    {result.engines.length > 0 && ` · ${result.engines.slice(0, 3).join(' / ')}${result.engines.length > 3 ? ` +${result.engines.length - 3}` : ''}`}
                  </Text>
                </div>
                <Paragraph
                  ellipsis={{ rows: 2, expandable: true, symbol: '展开' }}
                  style={{ marginTop: 4, marginBottom: 0, fontSize: 13 }}
                >
                  {result.snippet}
                </Paragraph>
                <a href={result.url} target="_blank" rel="noopener noreferrer" style={{ fontSize: 12 }}>
                  <LinkOutlined /> 打开原文
                </a>
              </div>
            </Space>
          </Card>
        );
      })}
    </Space>
  );
}
