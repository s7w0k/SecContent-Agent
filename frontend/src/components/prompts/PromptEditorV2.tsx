/**
 * 提示词编辑器 V2
 */
import {
  Alert,
  Button,
  Card,
  Col,
  Drawer,
  Input,
  Row,
  Space,
  Spin,
  Tag,
  Typography,
  message,
} from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { promptApi } from '../../api/client';
import type {
  PromptCatalogItem,
  PromptDetail,
  PromptValidationResult,
  PromptVersion,
} from '../../types';
import PromptCatalog from './PromptCatalog';

const { Text, Paragraph } = Typography;
const { TextArea } = Input;

interface PromptEditorV2Props {
  onDirtyChange?: (dirty: boolean) => void;
}

export default function PromptEditorV2({ onDirtyChange }: PromptEditorV2Props) {
  const [catalog, setCatalog] = useState<PromptCatalogItem[]>([]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [detail, setDetail] = useState<PromptDetail | null>(null);
  const [content, setContent] = useState('');
  const [dirty, setDirty] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [validation, setValidation] = useState<PromptValidationResult | null>(null);
  const [versionDrawerOpen, setVersionDrawerOpen] = useState(false);
  const [versions, setVersions] = useState<PromptVersion[]>([]);
  const [previewContent, setPreviewContent] = useState<string | null>(null);
  const [previewDrawerOpen, setPreviewDrawerOpen] = useState(false);

  const loadCatalog = useCallback(async () => {
    setLoading(true);
    try {
      const items = await promptApi.list();
      setCatalog(items);
      if (items.length > 0 && !selectedKey) {
        setSelectedKey(items[0].prompt_key);
      }
    } catch {
      message.error('加载提示词目录失败');
    } finally {
      setLoading(false);
    }
  }, [selectedKey]);

  useEffect(() => {
    void loadCatalog();
  }, [loadCatalog]);

  const loadDetail = useCallback(
    async (key: string) => {
      setLoading(true);
      try {
        const d = await promptApi.get(key);
        setDetail(d);
        setContent(d.content);
        setDirty(false);
        setValidation(null);
        onDirtyChange?.(false);
      } catch {
        message.error('加载提示词详情失败');
      } finally {
        setLoading(false);
      }
    },
    [onDirtyChange],
  );

  useEffect(() => {
    if (selectedKey) void loadDetail(selectedKey);
  }, [selectedKey, loadDetail]);

  const handleContentChange = (value: string) => {
    setContent(value);
    const isDirty = value !== detail?.content;
    setDirty(isDirty);
    onDirtyChange?.(isDirty);
  };

  const handleValidate = async () => {
    if (!selectedKey) return;
    try {
      const result = await promptApi.validate(selectedKey, content);
      setValidation(result);
      if (result.valid) {
        message.success('校验通过');
      } else {
        message.warning(`校验未通过：${result.errors.join('; ')}`);
      }
    } catch {
      message.error('校验失败');
    }
  };

  const handleSave = async () => {
    if (!selectedKey || !detail) return;
    setSaving(true);
    try {
      const saved = await promptApi.save(selectedKey, content, detail.version ?? undefined);
      setDetail(saved);
      setContent(saved.content);
      setDirty(false);
      onDirtyChange?.(false);
      setValidation(null);
      message.success('保存成功');
      void loadCatalog();
    } catch (error: unknown) {
      const err = error as { response?: { status?: number; data?: { detail?: string } } };
      if (err.response?.status === 409) {
        message.error('版本冲突，请重新加载');
        void loadDetail(selectedKey);
      } else {
        message.error(err.response?.data?.detail || '保存失败');
      }
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async () => {
    if (!selectedKey) return;
    try {
      const d = await promptApi.reset(selectedKey);
      setDetail(d);
      setContent(d.content);
      setDirty(false);
      onDirtyChange?.(false);
      message.success('已恢复系统默认');
      void loadCatalog();
    } catch {
      message.error('恢复失败');
    }
  };

  const handleShowVersions = async () => {
    if (!selectedKey) return;
    try {
      const result = await promptApi.listVersions(selectedKey);
      setVersions(result.items);
      setVersionDrawerOpen(true);
    } catch {
      message.error('加载历史版本失败');
    }
  };

  const handleRestoreVersion = async (version: number) => {
    if (!selectedKey) return;
    try {
      const d = await promptApi.restoreVersion(selectedKey, version);
      setDetail(d);
      setContent(d.content);
      setDirty(false);
      onDirtyChange?.(false);
      message.success(`已回滚到版本 ${version}`);
      void loadCatalog();
      setVersionDrawerOpen(false);
    } catch {
      message.error('回滚失败');
    }
  };

  const handlePreview = async () => {
    if (!selectedKey) return;
    try {
      const result = await promptApi.preview(selectedKey);
      setPreviewContent(result.composed_preview);
      setPreviewDrawerOpen(true);
    } catch {
      message.error('预览失败');
    }
  };

  return (
    <Row gutter={16} style={{ height: 'calc(100vh - 200px)' }}>
      <Col xs={24} md={8}>
        <Card title="提示词目录" size="small" styles={{ body: { padding: 0 } }}>
          <PromptCatalog
            items={catalog}
            selectedKey={selectedKey}
            onSelect={setSelectedKey}
            loading={loading}
          />
        </Card>
      </Col>
      <Col xs={24} md={16}>
        <Card
          title={
            <Space>
              <Text strong>{detail?.display_name || detail?.prompt_key || selectedKey}</Text>
              {detail && (
                <Tag color={detail.is_custom ? 'blue' : 'default'}>
                  {detail.is_custom ? `自定义 v${detail.version}` : '系统默认'}
                </Tag>
              )}
            </Space>
          }
          extra={
            <Space>
              <Button size="small" onClick={handleValidate}>
                校验
              </Button>
              <Button size="small" onClick={handlePreview}>
                预览
              </Button>
              <Button size="small" onClick={handleShowVersions}>
                历史
              </Button>
              <Button size="small" onClick={handleReset}>
                恢复默认
              </Button>
              <Button
                type="primary"
                size="small"
                loading={saving}
                onClick={handleSave}
                disabled={!dirty}
              >
                保存
              </Button>
            </Space>
          }
          size="small"
        >
          <Spin spinning={loading}>
            {detail && (
              <Space direction="vertical" size={12} style={{ width: '100%' }}>
                <Paragraph type="secondary" style={{ margin: 0, fontSize: 12 }}>
                  必需变量：
                  {detail.required_placeholders.map((p) => (
                    <Tag key={p} style={{ marginInlineStart: 0 }}>{`{${p}}`}</Tag>
                  ))}
                  {detail.required_placeholders.length === 0 && '无'}
                </Paragraph>
                {validation && (
                  <Alert
                    type={validation.valid ? 'success' : 'error'}
                    showIcon
                    message={validation.valid ? '校验通过' : validation.errors.join('; ')}
                    description={
                      validation.warnings.length > 0 ? validation.warnings.join('\n') : undefined
                    }
                    style={{ marginBottom: 8 }}
                  />
                )}
                <TextArea
                  value={content}
                  onChange={(e) => handleContentChange(e.target.value)}
                  rows={20}
                  style={{ fontFamily: 'monospace', fontSize: 13 }}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  字数：{content.length} · 预估 Token：~{Math.round(content.length * 1.5)}
                </Text>
              </Space>
            )}
          </Spin>
        </Card>
      </Col>
      <Drawer
        title="版本历史"
        open={versionDrawerOpen}
        onClose={() => setVersionDrawerOpen(false)}
        width={500}
      >
        {versions.map((v) => (
          <Card key={v.version_id} size="small" style={{ marginBottom: 8 }}>
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              <Space>
                <Tag color="blue">v{v.version}</Tag>
                <Tag>{v.change_type}</Tag>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {new Date(v.created_at).toLocaleString()}
                </Text>
              </Space>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {v.content_hash.slice(0, 24)}...
              </Text>
              <Button size="small" onClick={() => handleRestoreVersion(v.version)}>
                回滚到此版本
              </Button>
            </Space>
          </Card>
        ))}
      </Drawer>
      <Drawer
        title="组合预览"
        open={previewDrawerOpen}
        onClose={() => setPreviewDrawerOpen(false)}
        width={600}
      >
        <Text style={{ whiteSpace: 'pre-wrap', fontFamily: 'monospace', fontSize: 13 }}>
          {previewContent}
        </Text>
      </Drawer>
    </Row>
  );
}
