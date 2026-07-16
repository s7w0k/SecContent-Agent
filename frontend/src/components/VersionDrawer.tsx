import { HistoryOutlined, RollbackOutlined } from '@ant-design/icons';
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Empty,
  List,
  Modal,
  Pagination,
  Space,
  Tag,
  Typography,
  message,
} from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { prTemplateApi } from '../api/client';
import type { EffectivePRTemplate, PRTemplateVersion } from '../types';
import { templateErrorMessage } from '../utils/templateErrors';

const { Text } = Typography;
const PAGE_SIZE = 10;

const CHANGE_LABELS: Record<PRTemplateVersion['change_type'], string> = {
  create: '首次创建',
  update: '编辑保存',
  reset: '恢复默认',
  restore: '版本恢复',
};

interface VersionDrawerProps {
  open: boolean;
  template: EffectivePRTemplate | null;
  onClose: () => void;
  onRestored: (template: EffectivePRTemplate) => void;
}

export default function VersionDrawer({ open, template, onClose, onRestored }: VersionDrawerProps) {
  const [versions, setVersions] = useState<PRTemplateVersion[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [restoring, setRestoring] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!open || !template) return;
    setLoading(true);
    setError(null);
    try {
      const result = await prTemplateApi.versions(template.template_key, page, PAGE_SIZE);
      setVersions(result.items);
      setTotal(result.total);
    } catch (cause) {
      setError(templateErrorMessage(cause, '加载模板历史失败'));
    } finally {
      setLoading(false);
    }
  }, [open, page, template]);

  useEffect(() => {
    if (open) void load();
  }, [load, open]);

  const restore = async (version: number) => {
    if (!template) return;
    setRestoring(version);
    setError(null);
    try {
      const restored = await prTemplateApi.restore(template.template_key, version);
      onRestored(restored);
      message.success(`已将 v${version} 恢复为新版本 v${restored.version}`);
      await load();
    } catch (cause) {
      setError(templateErrorMessage(cause, '恢复历史版本失败'));
    } finally {
      setRestoring(null);
    }
  };

  return (
    <Drawer
      title={
        <Space>
          <HistoryOutlined />
          版本历史
        </Space>
      }
      open={open}
      onClose={() => {
        setPage(1);
        onClose();
      }}
      width={620}
    >
      {error && (
        <Alert type="error" showIcon closable message={error} onClose={() => setError(null)} />
      )}
      <Descriptions size="small" column={1} style={{ margin: '12px 0' }}>
        <Descriptions.Item label="当前模板">{template?.name || '-'}</Descriptions.Item>
        <Descriptions.Item label="当前版本">v{template?.version || 0}</Descriptions.Item>
      </Descriptions>
      <List
        loading={loading}
        dataSource={versions}
        locale={{ emptyText: <Empty description="暂无历史版本" /> }}
        renderItem={(item) => (
          <List.Item
            actions={[
              <Button
                key="restore"
                size="small"
                icon={<RollbackOutlined />}
                loading={restoring === item.version}
                onClick={() =>
                  Modal.confirm({
                    title: `恢复版本 v${item.version}？`,
                    content: '历史快照将保存为一个新的当前版本，不会覆盖历史记录。',
                    okText: '确认恢复',
                    cancelText: '取消',
                    onOk: () => restore(item.version),
                  })
                }
              >
                恢复此版本
              </Button>,
            ]}
          >
            <List.Item.Meta
              title={
                <Space wrap>
                  <Text strong>v{item.version}</Text>
                  <Tag>{CHANGE_LABELS[item.change_type]}</Tag>
                  <Text>{item.snapshot.name}</Text>
                </Space>
              }
              description={
                <Space direction="vertical" size={2}>
                  <Text type="secondary">
                    {new Date(item.created_at).toLocaleString()} · {item.snapshot.sections.length}{' '}
                    个章节
                  </Text>
                  <Text type="secondary" ellipsis>
                    {item.snapshot.title_template}
                  </Text>
                </Space>
              }
            />
          </List.Item>
        )}
      />
      {total > PAGE_SIZE && (
        <Pagination
          current={page}
          pageSize={PAGE_SIZE}
          total={total}
          showSizeChanger={false}
          onChange={setPage}
          style={{ marginTop: 16, textAlign: 'right' }}
        />
      )}
    </Drawer>
  );
}
