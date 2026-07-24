import { FundViewOutlined } from '@ant-design/icons';
import { Descriptions, Drawer, Empty, Space, Tag, Timeline, Typography } from 'antd';
import type { MemoryItem } from '../types';

const { Text } = Typography;

interface MemoryEvidenceDrawerProps {
  open: boolean;
  item: MemoryItem | null;
  onClose: () => void;
}

export default function MemoryEvidenceDrawer({ open, item, onClose }: MemoryEvidenceDrawerProps) {
  const confidencePercent = item ? Math.round(item.confidence * 100) : 0;

  return (
    <Drawer
      title={
        <Space>
          <FundViewOutlined />
          证据链
        </Space>
      }
      open={open}
      onClose={onClose}
      width={560}
    >
      {!item ? (
        <Empty description="未选择记忆" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Descriptions column={1} size="small" bordered>
            <Descriptions.Item label="展示文本">{item.display_text}</Descriptions.Item>
            <Descriptions.Item label="维度">{item.dimension}</Descriptions.Item>
            <Descriptions.Item label="极性">
              <Tag>{item.polarity}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label="状态">
              <Tag>{item.status}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label="置信度">{confidencePercent}%</Descriptions.Item>
            <Descriptions.Item label="作用域">
              <Space direction="vertical" size={0}>
                <Text type="secondary">分类：{item.scope.category_v2 || '-'}</Text>
                <Text type="secondary">模板：{item.scope.template_id || '-'}</Text>
                <Text type="secondary">阶段：{item.scope.stage || '-'}</Text>
              </Space>
            </Descriptions.Item>
            <Descriptions.Item label="支持/矛盾">
              <Space>
                <Text type="success">支持 {item.support_count}</Text>
                <Text type="danger">矛盾 {item.contradiction_count}</Text>
              </Space>
            </Descriptions.Item>
            <Descriptions.Item label="独立任务数">{item.independent_task_count}</Descriptions.Item>
          </Descriptions>

          <div>
            <Text strong>证据时间线</Text>
            {item.evidence_refs.length === 0 ? (
              <Empty description="暂无证据" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              <Timeline
                style={{ marginTop: 12 }}
                items={item.evidence_refs.map((ev) => ({
                  children: (
                    <Space direction="vertical" size={2}>
                      <Space>
                        <Tag>{ev.source_type}</Tag>
                        <Text type="secondary">权重 {ev.weight}</Text>
                      </Space>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {new Date(ev.observed_at).toLocaleString()}
                      </Text>
                    </Space>
                  ),
                }))}
              />
            )}
          </div>
        </Space>
      )}
    </Drawer>
  );
}
