/**
 * 修订记录列表组件
 *
 * 展示当前草稿下的历史修订记录，支持：
 * - 查看创建时间、修改意见和应用状态
 * - 点击切换查看某条修订稿
 * - 应用某个修订为当前主稿
 */

import { CheckCircleOutlined, EyeOutlined } from '@ant-design/icons';
import { Button, Empty, List, Space, Tag, Typography } from 'antd';
import type { DraftRevision } from '../types';

const { Text } = Typography;

interface RevisionListProps {
  revisions: DraftRevision[];
  selectedRevisionId: string | null;
  onSelect: (revision: DraftRevision) => void;
  onApply: (revision: DraftRevision) => void;
  applying: boolean;
}

export default function RevisionList({
  revisions,
  selectedRevisionId,
  onSelect,
  onApply,
  applying,
}: RevisionListProps) {
  if (!revisions || revisions.length === 0) {
    return <Empty description="暂无修订记录" image={Empty.PRESENTED_IMAGE_SIMPLE} />;
  }

  return (
    <List
      size="small"
      dataSource={revisions}
      renderItem={(rev, index) => (
        <List.Item
          style={{
            padding: '8px 12px',
            background: selectedRevisionId === rev.revision_id ? '#e6f4ff' : undefined,
            borderBottom: '1px solid #f0f0f0',
          }}
        >
          <div style={{ width: '100%' }}>
            <Space style={{ marginBottom: 4 }}>
              <Tag color={rev.applied ? 'green' : 'default'}>
                {rev.applied ? '已应用' : `v${revisions.length - index}`}
              </Tag>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {rev.created_at?.slice(0, 19).replace('T', ' ') || '未知时间'}
              </Text>
            </Space>
            <Text style={{ display: 'block', fontSize: 13 }} ellipsis>
              {rev.instruction}
            </Text>
            {rev.change_summary?.length > 0 && (
              <div style={{ marginTop: 2 }}>
                {rev.change_summary.slice(0, 3).map((s, i) => (
                  // biome-ignore lint/suspicious/noArrayIndexKey: 变更摘要可能重复，索引 key 保持渲染稳定
                  <Tag key={i} style={{ fontSize: 11, marginBottom: 2 }}>
                    {s}
                  </Tag>
                ))}
              </div>
            )}
            <Space style={{ marginTop: 4 }}>
              <Button size="small" type="link" icon={<EyeOutlined />} onClick={() => onSelect(rev)}>
                查看
              </Button>
              {!rev.applied && (
                <Button
                  size="small"
                  type="link"
                  icon={<CheckCircleOutlined />}
                  onClick={() => onApply(rev)}
                  loading={applying}
                  style={{ color: '#52c41a' }}
                >
                  应用为当前稿
                </Button>
              )}
            </Space>
          </div>
        </List.Item>
      )}
    />
  );
}
