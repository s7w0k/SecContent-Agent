/**
 * 生成配置弹窗 - 产品选择 + 参考稿上传
 */
import { InboxOutlined } from '@ant-design/icons';
import { Alert, Modal, Radio, Select, Space, Switch, Typography, Upload, message } from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { userKnowledgeApi } from '../../api/client';
import type { UserProductListItem } from '../../types';

const { Text, Paragraph } = Typography;

const MAX_TEMPLATE_FILES = 3;
const MAX_TEMPLATE_CHARS = 15000;

export interface GenerationConfig {
  product_relevance_enabled: boolean;
  product_target_mode: 'none' | 'auto' | 'selected';
  selected_product_ids: string[];
  force_generate: boolean;
  draft_variants: 1 | 2 | 4;
  reference_template?: string;
}

interface GenerationConfigModalProps {
  open: boolean;
  onCancel: () => void;
  onConfirm: (config: GenerationConfig) => void;
  articleScore?: number;
  articleThreshold?: number;
  loading?: boolean;
}

export default function GenerationConfigModal({
  open,
  onCancel,
  onConfirm,
  articleScore,
  articleThreshold = 80,
  loading = false,
}: GenerationConfigModalProps) {
  const [products, setProducts] = useState<UserProductListItem[]>([]);
  const [mode, setMode] = useState<'none' | 'auto' | 'selected'>('selected');
  const [relevanceEnabled, setRelevanceEnabled] = useState(true);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [forceGenerate, setForceGenerate] = useState(false);
  const [draftVariants, setDraftVariants] = useState<1 | 2 | 4>(1);
  const [templates, setTemplates] = useState<{ name: string; text: string }[]>([]);
  const [showBelowThresholdConfirm, setShowBelowThresholdConfirm] = useState(false);

  const loadProducts = useCallback(async () => {
    try {
      const items = await userKnowledgeApi.listProducts();
      setProducts(items);
      // 默认选中所有已发布产品
      setSelectedIds(items.map((p) => p.product_id));
    } catch {
      // 静默失败，用户仍可使用 auto 模式
    }
  }, []);

  useEffect(() => {
    if (open) void loadProducts();
  }, [open, loadProducts]);

  const isBelowThreshold = articleScore !== undefined && articleScore < articleThreshold;

  const handleConfirm = () => {
    if (isBelowThreshold && !forceGenerate && !showBelowThresholdConfirm) {
      setShowBelowThresholdConfirm(true);
      return;
    }
    setShowBelowThresholdConfirm(false);
    const referenceTemplate =
      templates.length > 0
        ? templates
            .map((t, i) => `### 参考稿件 ${i + 1}：${t.name}\n\n${t.text}`)
            .join('\n\n---\n\n')
        : undefined;
    onConfirm({
      product_relevance_enabled: mode === 'none' ? false : relevanceEnabled,
      product_target_mode: mode,
      selected_product_ids: mode === 'selected' ? selectedIds : [],
      force_generate: forceGenerate,
      draft_variants: draftVariants,
      reference_template: referenceTemplate,
    });
  };

  const handleCancel = () => {
    setShowBelowThresholdConfirm(false);
    setMode('selected');
    setRelevanceEnabled(true);
    setSelectedIds([]);
    setForceGenerate(false);
    setDraftVariants(1);
    setTemplates([]);
    onCancel();
  };

  return (
    <Modal
      title="生成草稿"
      open={open}
      onOk={handleConfirm}
      onCancel={handleCancel}
      confirmLoading={loading}
      okText={showBelowThresholdConfirm ? '确认生成' : '开始生成'}
      cancelText="取消"
      width={560}
    >
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        {showBelowThresholdConfirm && isBelowThreshold && (
          <Alert
            type="warning"
            showIcon
            message="文章未达到 PR 候选标准"
            description={`当前分数 ${articleScore}，阈值 ${articleThreshold}。确认要强制生成吗？`}
            action={
              <Switch
                checked={forceGenerate}
                onChange={setForceGenerate}
                checkedChildren="强制"
                unCheckedChildren="取消"
              />
            }
          />
        )}

        <div>
          <Text strong style={{ display: 'block', marginBottom: 8 }}>
            关联产品模式
          </Text>
          <Radio.Group
            value={mode}
            onChange={(e) => {
              const m = e.target.value;
              setMode(m);
              if (m === 'none') setRelevanceEnabled(false);
              if (m === 'selected' && selectedIds.length === 0 && products.length > 0) {
                setSelectedIds(products.map((p) => p.product_id));
              }
            }}
          >
            <Radio value="none">不关联产品</Radio>
            <Radio value="auto">自动匹配</Radio>
            <Radio value="selected">指定产品</Radio>
          </Radio.Group>
        </div>

        {mode !== 'none' && (
          <div>
            <Space>
              <Switch checked={relevanceEnabled} onChange={setRelevanceEnabled} size="small" />
              <Text type="secondary">启用产品相关性评分</Text>
            </Space>
          </div>
        )}

        {mode === 'selected' && (
          <div>
            <Text strong style={{ display: 'block', marginBottom: 8 }}>
              选择产品（最多 5 个，已默认全选）
            </Text>
            <Select
              mode="multiple"
              style={{ width: '100%' }}
              value={selectedIds}
              onChange={setSelectedIds}
              maxCount={5}
              placeholder="请选择产品"
            >
              {products.map((p) => (
                <Select.Option key={p.product_id} value={p.product_id}>
                  {p.name}
                </Select.Option>
              ))}
            </Select>
          </div>
        )}

        <div>
          <Text strong style={{ display: 'block', marginBottom: 8 }}>
            生成版本数
          </Text>
          <Radio.Group
            value={draftVariants}
            onChange={(event) => setDraftVariants(event.target.value)}
            optionType="button"
            buttonStyle="solid"
          >
            <Radio.Button value={1}>1 个首稿（推荐）</Radio.Button>
            <Radio.Button value={2}>2 个备选</Radio.Button>
            <Radio.Button value={4}>4 个全量</Radio.Button>
          </Radio.Group>
          <Paragraph type="secondary" style={{ fontSize: 12, margin: '8px 0 0' }}>
            每增加一个版本都会增加一次写稿和一次内容检查；首稿模式耗时和 Token 最低。
          </Paragraph>
        </div>

        <div>
          <Text strong style={{ display: 'block', marginBottom: 8 }}>
            参考稿件（可选，最多 {MAX_TEMPLATE_FILES} 篇）
          </Text>
          <Upload.Dragger
            accept=".txt,.md,.markdown"
            maxCount={MAX_TEMPLATE_FILES}
            multiple
            fileList={templates.map((t, i) => ({
              uid: String(i),
              name: t.name,
              status: 'done' as const,
            }))}
            beforeUpload={(file) => {
              if (templates.length >= MAX_TEMPLATE_FILES) {
                message.warning(`最多上传 ${MAX_TEMPLATE_FILES} 篇`);
                return false;
              }
              const reader = new FileReader();
              reader.onload = (e) => {
                const text = e.target?.result as string;
                const totalChars = templates.reduce((s, t) => s + t.text.length, 0) + text.length;
                if (totalChars > MAX_TEMPLATE_CHARS) {
                  message.error(
                    `总字符数超出限制（${totalChars}/${MAX_TEMPLATE_CHARS}），请减少或缩短文件`,
                  );
                  return;
                }
                setTemplates((prev) => [...prev, { name: file.name, text }]);
                message.success(`已加载: ${file.name}（${text.length} 字符）`);
              };
              reader.readAsText(file);
              return false;
            }}
            onRemove={(file) => {
              setTemplates((prev) => prev.filter((_, i) => String(i) !== file.uid));
            }}
          >
            <p className="ant-upload-drag-icon">
              <InboxOutlined />
            </p>
            <p className="ant-upload-text">点击或拖拽文件到此处</p>
            <p className="ant-upload-hint">
              最多 {MAX_TEMPLATE_FILES} 篇，总字符 ≤ {MAX_TEMPLATE_CHARS}，支持 .txt / .md
            </p>
          </Upload.Dragger>
          {templates.length > 0 && (
            <Paragraph type="success" style={{ marginTop: 8, marginBottom: 0, fontSize: 13 }}>
              已加载 {templates.length} 篇参考模板（共{' '}
              {templates.reduce((s, t) => s + t.text.length, 0)} 字符），将注入生成上下文。
            </Paragraph>
          )}
        </div>

        {mode === 'none' && (
          <Paragraph type="secondary" style={{ fontSize: 12, margin: 0 }}>
            不关联产品模式：候选分 = 事件影响力（0~100），不注入产品知识库
          </Paragraph>
        )}
        {mode === 'auto' && relevanceEnabled && (
          <Paragraph type="secondary" style={{ fontSize: 12, margin: 0 }}>
            自动匹配模式：系统根据文章内容自动匹配产品，候选分 = 产品相关度 + 事件影响力（0~200）
          </Paragraph>
        )}
        {mode === 'auto' && !relevanceEnabled && (
          <Paragraph type="secondary" style={{ fontSize: 12, margin: 0 }}>
            自动匹配但不评分产品：候选分 = 事件影响力（0~100）
          </Paragraph>
        )}
        {mode === 'selected' && (
          <Paragraph type="secondary" style={{ fontSize: 12, margin: 0 }}>
            指定产品模式：仅注入选中产品的知识库，候选分 = 产品相关度 + 事件影响力（0~200）
          </Paragraph>
        )}
      </Space>
    </Modal>
  );
}
