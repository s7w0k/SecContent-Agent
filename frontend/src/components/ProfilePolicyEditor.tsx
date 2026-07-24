import { ReloadOutlined, SaveOutlined } from '@ant-design/icons';
import {
  Alert,
  Button,
  Card,
  Divider,
  Form,
  Input,
  Popconfirm,
  Select,
  Skeleton,
  Space,
  Switch,
  Tag,
  Typography,
  message,
} from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { policyApi } from '../api/client';
import type { PolicyUpdateRequest, ProfilePolicy } from '../types';

const { Text } = Typography;

// ── PR 稿内容生成偏好选项 ──────────────────────────

const CONTENT_FOCUS_OPTIONS = [
  { value: 'product_tech', label: '产品技术亮点' },
  { value: 'industry_trend', label: '行业趋势洞察' },
  { value: 'customer_value', label: '客户案例价值' },
  { value: 'brand_authority', label: '品牌权威定位' },
  { value: 'solution_advantage', label: '解决方案优势' },
];

const OPENING_STYLE_OPTIONS = [
  { value: 'hot_topic', label: '热点切入' },
  { value: 'data', label: '数据切入' },
  { value: 'question', label: '问题切入' },
  { value: 'direct_product', label: '直述产品' },
];

const STRUCTURE_OPTIONS = [
  { value: 'inverted_pyramid', label: '倒金字塔（新闻式：核心在前，细节递减）' },
  { value: 'problem_solution', label: '问题-方案-总结（总分总）' },
  { value: 'storytelling', label: '故事线（场景叙事）' },
  { value: 'progressive', label: '递进式（层层深入）' },
];

const REQUIRED_PATTERN_SUGGESTIONS = [
  '品牌全称',
  '产品官方名称',
  '官网链接',
  '免责声明',
  '数据来源标注',
];

const AVOID_PATTERN_SUGGESTIONS = [
  '竞品名称',
  '绝对化用语',
  '未经验证的数据',
  '敏感话题',
  '负面表述',
];

// ── 错误处理 ──────────────────────────────────────

interface PolicyApiError {
  response?: {
    status?: number;
    data?: {
      detail?: string | { message?: string } | Array<{ msg?: string }>;
      error?: { message?: string };
    };
  };
  message?: string;
}

function policyErrorMessage(error: unknown, fallback: string): string {
  const candidate = error as PolicyApiError;
  const detail = candidate.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const messages = detail.map((item) => item.msg).filter(Boolean);
    if (messages.length > 0) return messages.join('；');
  }
  return (
    candidate.response?.data?.error?.message ||
    (!Array.isArray(detail) ? detail?.message : undefined) ||
    candidate.message ||
    fallback
  );
}

// ── 表单值 ────────────────────────────────────────

interface FormValues {
  content_focus: string[];
  opening_style: string | null;
  structure_preference: string | null;
  required_patterns: string[];
  avoid_patterns: string[];
  custom_instructions: string | null;
  auto_learning_enabled: boolean;
  memory_write_approval: boolean;
}

function toFormValues(policy: ProfilePolicy): FormValues {
  return {
    content_focus: policy.content_focus ?? [],
    opening_style: policy.opening_style,
    structure_preference: policy.structure_preference,
    required_patterns: policy.required_patterns ?? [],
    avoid_patterns: policy.avoid_patterns ?? [],
    custom_instructions: policy.custom_instructions,
    auto_learning_enabled: policy.auto_learning_enabled,
    memory_write_approval: policy.memory_write_approval,
  };
}

// ── 组件 ──────────────────────────────────────────

export default function ProfilePolicyEditor() {
  const [form] = Form.useForm<FormValues>();
  const [policy, setPolicy] = useState<ProfilePolicy | null>(null);
  const [version, setVersion] = useState(0);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const applyPolicy = useCallback(
    (nextPolicy: ProfilePolicy, nextVersion: number) => {
      setPolicy(nextPolicy);
      setVersion(nextVersion);
      form.setFieldsValue(toFormValues(nextPolicy));
    },
    [form],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await policyApi.getPolicy();
      applyPolicy(res.data.policy, res.data.version);
    } catch (cause) {
      setError(policyErrorMessage(cause, '加载偏好策略失败'));
    } finally {
      setLoading(false);
    }
  }, [applyPolicy]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleSave = async () => {
    let values: FormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const payload: PolicyUpdateRequest = {
        content_focus: values.content_focus,
        opening_style: values.opening_style || null,
        structure_preference: values.structure_preference || null,
        required_patterns: values.required_patterns,
        avoid_patterns: values.avoid_patterns,
        custom_instructions: values.custom_instructions || null,
        auto_learning_enabled: values.auto_learning_enabled,
        memory_write_approval: values.memory_write_approval,
      };
      const res = await policyApi.savePolicy(payload, version);
      applyPolicy(res.data.policy, res.data.version);
      message.success('偏好策略已保存');
    } catch (cause) {
      const status = (cause as PolicyApiError).response?.status;
      if (status === 409) {
        message.error('版本冲突：策略已被其他会话更新，正在重新加载');
        await load();
      } else {
        setError(policyErrorMessage(cause, '保存偏好策略失败'));
      }
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async () => {
    setResetting(true);
    setError(null);
    try {
      const res = await policyApi.resetPolicy();
      applyPolicy(res.data.policy, res.data.version);
      message.success('已恢复默认偏好策略');
    } catch (cause) {
      setError(policyErrorMessage(cause, '恢复默认偏好策略失败'));
    } finally {
      setResetting(false);
    }
  };

  if (loading) return <Skeleton active paragraph={{ rows: 12 }} />;

  if (!policy) {
    return (
      <Alert
        showIcon
        type="error"
        message="偏好策略加载失败"
        description={error}
        action={<Button onClick={load}>重新加载</Button>}
      />
    );
  }

  return (
    <Card
      title="PR 稿生成偏好"
      extra={<Tag color={version > 0 ? 'green' : 'default'}>v{version}</Tag>}
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Text type="secondary">
          配置 PR 初稿生成的内容侧重、标题风格、开篇方式与结构偏好，LLM 将在生成时遵循这些偏好。保存时携带版本号，若策略已被其他会话更新将提示冲突。
        </Text>
        {error && (
          <Alert
            showIcon
            closable
            type="error"
            message="偏好策略操作失败"
            description={error}
            onClose={() => setError(null)}
          />
        )}
        <Form
          form={form}
          layout="vertical"
          initialValues={policy ? toFormValues(policy) : undefined}
        >
          <Form.Item
            label="内容侧重方向"
            name="content_focus"
            tooltip="选择 PR 稿应重点突出的内容方向，可多选"
          >
            <Select
              mode="multiple"
              placeholder="选择内容侧重方向（可多选）"
              options={CONTENT_FOCUS_OPTIONS}
              optionFilterProp="label"
            />
          </Form.Item>

          <Form.Item
            label="开篇方式"
            name="opening_style"
            tooltip="PR 稿正文开头的切入方式"
          >
            <Select allowClear placeholder="选择开篇方式" options={OPENING_STYLE_OPTIONS} />
          </Form.Item>

          <Form.Item
            label="结构偏好"
            name="structure_preference"
            tooltip="PR 稿整体行文结构"
          >
            <Select allowClear placeholder="选择结构偏好" options={STRUCTURE_OPTIONS} />
          </Form.Item>

          <Divider style={{ margin: '8px 0' }} />

          <Form.Item
            label="自定义偏好说明"
            name="custom_instructions"
            tooltip="用自然语言描述你对 PR 稿生成的其他偏好要求，LLM 将参考这段描述"
          >
            <Input.TextArea
              rows={4}
              maxLength={2000}
              showCount
              placeholder="例如：标题中尽量包含数据指标；正文每段不超过 3 句话；结尾需要有行动号召；避免使用感叹号等"
            />
          </Form.Item>

          <Divider style={{ margin: '8px 0' }} />

          <Form.Item
            label="必含要素"
            name="required_patterns"
            tooltip="PR 稿中必须包含的要素，如品牌全称、免责声明等，输入后按回车添加"
          >
            <Select
              mode="tags"
              placeholder="输入必含要素后按回车添加"
              tokenSeparators={[',']}
              options={REQUIRED_PATTERN_SUGGESTIONS.map((v) => ({ value: v, label: v }))}
            />
          </Form.Item>

          <Form.Item
            label="规避要素"
            name="avoid_patterns"
            tooltip="PR 稿中应避免的要素，如竞品名称、绝对化用语等，输入后按回车添加"
          >
            <Select
              mode="tags"
              placeholder="输入规避要素后按回车添加"
              tokenSeparators={[',']}
              options={AVOID_PATTERN_SUGGESTIONS.map((v) => ({ value: v, label: v }))}
            />
          </Form.Item>

          <Divider style={{ margin: '8px 0' }} />

          <Form.Item
            label="自动学习"
            name="auto_learning_enabled"
            valuePropName="checked"
            tooltip="开启后，系统将从你的反馈和改稿中自动学习偏好"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            label="记忆写入需审批"
            name="memory_write_approval"
            valuePropName="checked"
            tooltip="开启后，自动学习的偏好需手动审批后才生效"
          >
            <Switch />
          </Form.Item>

          <Space>
            <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={handleSave}>
              保存策略
            </Button>
            <Popconfirm
              title="恢复默认策略？"
              description="将清除所有自定义偏好，确认恢复？"
              okText="确认恢复"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={handleReset}
            >
              <Button icon={<ReloadOutlined />} loading={resetting}>
                恢复默认
              </Button>
            </Popconfirm>
          </Space>
        </Form>
      </Space>
    </Card>
  );
}
