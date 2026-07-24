import {
  CheckOutlined,
  CloseOutlined,
  EditOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
} from '@ant-design/icons';
import { Button, Card, Progress, Space, Tag, Tooltip, Typography } from 'antd';
import type { ReactNode } from 'react';
import type { MemoryItem } from '../types';

const { Text } = Typography;

const POLARITY_COLOR: Record<MemoryItem['polarity'], string> = {
  prefer: 'green',
  avoid: 'red',
  require: 'purple',
};

const POLARITY_LABEL: Record<MemoryItem['polarity'], string> = {
  prefer: '偏好',
  avoid: '规避',
  require: '必含',
};

const STATUS_COLOR: Record<MemoryItem['status'], string> = {
  candidate: 'default',
  pending_approval: 'processing',
  active: 'success',
  suppressed: 'default',
  expired: 'default',
  rejected: 'error',
};

const STATUS_LABEL: Record<MemoryItem['status'], string> = {
  candidate: '候选',
  pending_approval: '待确认',
  active: '生效中',
  suppressed: '已停用',
  expired: '已过期',
  rejected: '已拒绝',
};

interface MemoryItemCardProps {
  item: MemoryItem;
  onAction: (action: string) => void;
}

function buildActions(
  status: MemoryItem['status'],
  onAction: (action: string) => void,
): ReactNode[] {
  const actions: ReactNode[] = [];

  if (status !== 'rejected' && status !== 'expired') {
    actions.push(
      <Button key="edit" size="small" icon={<EditOutlined />} onClick={() => onAction('edit')}>
        编辑
      </Button>,
    );
  }

  switch (status) {
    case 'active':
      actions.push(
        <Button
          key="suppress"
          size="small"
          icon={<PauseCircleOutlined />}
          onClick={() => onAction('suppress')}
        >
          停用
        </Button>,
      );
      break;
    case 'pending_approval':
      actions.push(
        <Button
          key="approve"
          size="small"
          type="primary"
          icon={<CheckOutlined />}
          onClick={() => onAction('approve')}
        >
          确认
        </Button>,
      );
      actions.push(
        <Button
          key="reject"
          size="small"
          danger
          icon={<CloseOutlined />}
          onClick={() => onAction('reject')}
        >
          拒绝
        </Button>,
      );
      break;
    case 'suppressed':
      actions.push(
        <Button
          key="activate"
          size="small"
          icon={<PlayCircleOutlined />}
          onClick={() => onAction('activate')}
        >
          恢复
        </Button>,
      );
      break;
    default:
      break;
  }

  return actions;
}

export default function MemoryItemCard({ item, onAction }: MemoryItemCardProps) {
  const confidencePercent = Math.round(item.confidence * 100);
  const actions = buildActions(item.status, onAction);

  return (
    <Card
      size="small"
      title={
        <Space wrap>
          <Tooltip title={`极性：${POLARITY_LABEL[item.polarity]}`}>
            <Tag color={POLARITY_COLOR[item.polarity]}>{item.dimension}</Tag>
          </Tooltip>
          <Tag color={STATUS_COLOR[item.status]}>{STATUS_LABEL[item.status]}</Tag>
        </Space>
      }
      extra={
        <Text type="secondary" style={{ fontSize: 12 }}>
          v{item.version}
        </Text>
      }
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Text>{item.display_text}</Text>

        <Progress
          percent={confidencePercent}
          size="small"
          status={item.contradiction_count > 0 ? 'exception' : 'normal'}
          format={() => `置信度 ${confidencePercent}%`}
        />

        <Space size={[16, 0]} wrap>
          <Tooltip title="支持证据数">
            <Text type="secondary">支持 {item.support_count}</Text>
          </Tooltip>
          <Tooltip title="矛盾证据数">
            <Text type={item.contradiction_count > 0 ? 'danger' : 'secondary'}>
              矛盾 {item.contradiction_count}
            </Text>
          </Tooltip>
          <Tooltip title="独立任务数">
            <Text type="secondary">独立任务 {item.independent_task_count}</Text>
          </Tooltip>
          <Text type="secondary">使用 {item.use_count} 次</Text>
        </Space>

        {actions.length > 0 && <Space wrap>{actions}</Space>}
      </Space>
    </Card>
  );
}
