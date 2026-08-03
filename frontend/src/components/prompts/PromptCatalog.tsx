/**
 * 提示词目录列表
 */
import { List, Space, Tag, Typography } from 'antd';
import type { PromptCatalogItem } from '../../types';

const { Text } = Typography;

interface PromptCatalogProps {
  items: PromptCatalogItem[];
  selectedKey: string | null;
  onSelect: (key: string) => void;
  loading?: boolean;
}

const STAGE_LABELS: Record<string, string> = {
  classify: '分类',
  score: '评分',
  draft: '初稿',
  chat: '对话',
  review: '审核',
};

export default function PromptCatalog({
  items,
  selectedKey,
  onSelect,
  loading,
}: PromptCatalogProps) {
  return (
    <List
      loading={loading}
      dataSource={items}
      renderItem={(item) => (
        <List.Item
          style={{
            cursor: 'pointer',
            padding: '12px 16px',
            background: selectedKey === item.prompt_key ? '#e6f4ff' : undefined,
            borderLeft:
              selectedKey === item.prompt_key ? '3px solid #1677ff' : '3px solid transparent',
          }}
          onClick={() => onSelect(item.prompt_key)}
        >
          <Space direction="vertical" size={2} style={{ width: '100%' }}>
            <Space>
              <Text strong>{item.display_name}</Text>
              <Tag color={item.is_custom ? 'blue' : 'default'} style={{ marginInlineStart: 0 }}>
                {item.is_custom ? `自定义 v${item.version}` : '系统默认'}
              </Tag>
            </Space>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {STAGE_LABELS[item.stage] || item.stage} · {item.description}
            </Text>
            {item.updated_at && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                更新于 {new Date(item.updated_at).toLocaleString()}
              </Text>
            )}
          </Space>
        </List.Item>
      )}
    />
  );
}
