import { EditOutlined, HistoryOutlined, ReloadOutlined, RollbackOutlined } from '@ant-design/icons';
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Modal,
  Row,
  Skeleton,
  Space,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { prTemplateApi } from '../api/client';
import TemplateEditor from '../components/TemplateEditor';
import VersionDrawer from '../components/VersionDrawer';
import type { EffectivePRTemplate, PRTemplateCategory } from '../types';
import { templateErrorMessage } from '../utils/templateErrors';

const { Paragraph, Text, Title } = Typography;

const CATEGORIES: Array<{ key: PRTemplateCategory; label: string; description: string }> = [
  { key: '爆点事件', label: '爆点事件', description: '重大安全事件、漏洞与行业热点' },
  { key: '法律法规/监管动态', label: '法律法规 / 监管', description: '法规政策与监管要求解读' },
  { key: 'AI技术重大进展', label: 'AI 技术重大进展', description: 'AI 与智能体安全技术趋势' },
];

interface PRTemplatesPageProps {
  onDirtyChange?: (dirty: boolean) => void;
}

export default function PRTemplatesPage({ onDirtyChange }: PRTemplatesPageProps) {
  const [templates, setTemplates] = useState<EffectivePRTemplate[]>([]);
  const [category, setCategory] = useState<PRTemplateCategory>('爆点事件');
  const [editing, setEditing] = useState<EffectivePRTemplate | null>(null);
  const [historyTemplate, setHistoryTemplate] = useState<EffectivePRTemplate | null>(null);
  const [editorDirty, setEditorDirty] = useState(false);
  const [loading, setLoading] = useState(true);
  const [resetting, setResetting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const setDirty = useCallback(
    (dirty: boolean) => {
      setEditorDirty(dirty);
      onDirtyChange?.(dirty);
    },
    [onDirtyChange],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await prTemplateApi.list();
      setTemplates(result.items);
    } catch (cause) {
      setError(templateErrorMessage(cause, '加载 PR 模板失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const guard = (event: BeforeUnloadEvent) => {
      if (!editorDirty) return;
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', guard);
    return () => window.removeEventListener('beforeunload', guard);
  }, [editorDirty]);

  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  const visibleTemplates = useMemo(
    () => templates.filter((template) => template.category_v2 === category),
    [category, templates],
  );

  const updateTemplate = (updated: EffectivePRTemplate) => {
    setTemplates((current) =>
      current.map((template) =>
        template.template_key === updated.template_key ? updated : template,
      ),
    );
    setEditing((current) => (current?.template_key === updated.template_key ? updated : current));
    setHistoryTemplate((current) =>
      current?.template_key === updated.template_key ? updated : current,
    );
  };

  const confirmDiscard = (next: () => void) => {
    if (!editorDirty) {
      next();
      return;
    }
    Modal.confirm({
      title: '放弃未保存的修改？',
      content: '当前模板还有未保存内容，继续操作将丢失这些修改。',
      okText: '放弃修改',
      cancelText: '继续编辑',
      okButtonProps: { danger: true },
      onOk: () => {
        setDirty(false);
        next();
      },
    });
  };

  const reset = async (template: EffectivePRTemplate) => {
    setResetting(template.template_key);
    setError(null);
    try {
      const restored = await prTemplateApi.reset(template.template_key);
      updateTemplate(restored);
      message.success('已恢复系统默认模板');
    } catch (cause) {
      setError(templateErrorMessage(cause, '恢复系统默认失败'));
    } finally {
      setResetting(null);
    }
  };

  return (
    <div style={{ padding: 24, background: '#f5f7fb', minHeight: 'calc(100vh - 64px)' }}>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Col>
          <Title level={3} style={{ margin: 0 }}>
            PR 模板
          </Title>
          <Paragraph type="secondary" style={{ margin: 0 }}>
            按账号维护六套个性化模板；未自定义的槽位自动使用系统默认配置。
          </Paragraph>
        </Col>
        <Col>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>
            刷新模板
          </Button>
        </Col>
      </Row>

      {error && (
        <Alert
          showIcon
          closable
          type="error"
          message="模板操作失败"
          description={error}
          onClose={() => setError(null)}
          style={{ marginBottom: 16 }}
        />
      )}

      <Card>
        <Tabs
          activeKey={category}
          items={CATEGORIES.map((item) => ({
            key: item.key,
            label: item.label,
            children: <Text type="secondary">{item.description}</Text>,
          }))}
          onChange={(next) =>
            confirmDiscard(() => {
              setEditing(null);
              setCategory(next as PRTemplateCategory);
            })
          }
        />

        {loading ? (
          <Skeleton active paragraph={{ rows: 8 }} />
        ) : visibleTemplates.length === 0 ? (
          <Empty description="该分类暂无模板" />
        ) : (
          <Row gutter={[16, 16]}>
            {visibleTemplates.map((template) => (
              <Col xs={24} lg={12} key={template.template_key}>
                <Card
                  title={
                    <Space>
                      <Tag color="blue">槽位 {template.slot}</Tag>
                      {template.name}
                    </Space>
                  }
                  extra={
                    <Tag color={template.source === 'user' ? 'green' : 'default'}>
                      {template.source === 'user' ? '用户自定义' : '系统默认'}
                    </Tag>
                  }
                  actions={[
                    <Button
                      key="edit"
                      type="link"
                      icon={<EditOutlined />}
                      onClick={() => confirmDiscard(() => setEditing(template))}
                    >
                      编辑
                    </Button>,
                    <Button
                      key="history"
                      type="link"
                      icon={<HistoryOutlined />}
                      onClick={() => confirmDiscard(() => setHistoryTemplate(template))}
                    >
                      历史
                    </Button>,
                    <Button
                      key="reset"
                      type="link"
                      danger
                      disabled={template.source === 'system'}
                      loading={resetting === template.template_key}
                      icon={<RollbackOutlined />}
                      onClick={() =>
                        Modal.confirm({
                          title: '恢复系统默认模板？',
                          content: '当前用户自定义内容将停用，但历史版本仍会保留。',
                          okText: '恢复默认',
                          cancelText: '取消',
                          onOk: () => reset(template),
                        })
                      }
                    >
                      恢复默认
                    </Button>,
                  ]}
                >
                  <Descriptions size="small" column={1}>
                    <Descriptions.Item label="模板键">{template.template_key}</Descriptions.Item>
                    <Descriptions.Item label="版本">v{template.version}</Descriptions.Item>
                    <Descriptions.Item label="章节">
                      {template.sections.length} 个
                    </Descriptions.Item>
                    <Descriptions.Item label="视角">
                      <Space wrap>
                        {template.perspectives.map((item) => (
                          <Tag key={item}>{item}</Tag>
                        ))}
                      </Space>
                    </Descriptions.Item>
                    <Descriptions.Item label="更新时间">
                      {template.updated_at
                        ? new Date(template.updated_at).toLocaleString()
                        : '随系统发布'}
                    </Descriptions.Item>
                  </Descriptions>
                </Card>
              </Col>
            ))}
          </Row>
        )}
      </Card>

      <Drawer
        title={editing ? `编辑模板 · ${editing.name}` : '编辑模板'}
        open={editing !== null}
        width={780}
        onClose={() =>
          confirmDiscard(() => {
            setDirty(false);
            setEditing(null);
          })
        }
        destroyOnClose
      >
        {editing && (
          <TemplateEditor
            key={`${editing.template_key}:${editing.version}`}
            template={editing}
            onDirtyChange={setDirty}
            onSaved={updateTemplate}
          />
        )}
      </Drawer>

      <VersionDrawer
        open={historyTemplate !== null}
        template={historyTemplate}
        onClose={() => setHistoryTemplate(null)}
        onRestored={updateTemplate}
      />
    </div>
  );
}
