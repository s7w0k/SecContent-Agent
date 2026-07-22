import { ReloadOutlined, SaveOutlined } from '@ant-design/icons';
import {
  Alert,
  Button,
  Card,
  Input,
  Popconfirm,
  Skeleton,
  Space,
  Tag,
  Typography,
  message,
} from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { promptApi } from '../api/client';
import type { EffectivePrompt } from '../types';

const { Paragraph, Text } = Typography;
const { TextArea } = Input;

interface PromptEditorProps {
  onDirtyChange?: (dirty: boolean) => void;
}

interface PromptApiError {
  response?: {
    data?: {
      detail?: string | { message?: string } | Array<{ msg?: string }>;
      error?: { message?: string };
    };
  };
  message?: string;
}

function promptErrorMessage(error: unknown, fallback: string): string {
  const candidate = error as PromptApiError;
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

export function findMissingPlaceholders(content: string, required: string[]): string[] {
  return required.filter((placeholder) => !content.includes(`{${placeholder}}`));
}

export default function PromptEditor({ onDirtyChange }: PromptEditorProps) {
  const [prompt, setPrompt] = useState<EffectivePrompt | null>(null);
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const dirty = prompt !== null && content !== prompt.content;
  const missingPlaceholders = useMemo(
    () => findMissingPlaceholders(content, prompt?.required_placeholders ?? []),
    [content, prompt?.required_placeholders],
  );

  const applyPrompt = useCallback((nextPrompt: EffectivePrompt) => {
    setPrompt(nextPrompt);
    setContent(nextPrompt.content);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      applyPrompt(await promptApi.getDraftPrompt());
    } catch (cause) {
      setError(promptErrorMessage(cause, '加载初稿提示词失败'));
    } finally {
      setLoading(false);
    }
  }, [applyPrompt]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  useEffect(() => {
    const guard = (event: BeforeUnloadEvent) => {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', guard);
    return () => window.removeEventListener('beforeunload', guard);
  }, [dirty]);

  const save = async () => {
    if (missingPlaceholders.length > 0) {
      setError(
        `请保留以下必需占位符：${missingPlaceholders
          .map((placeholder) => `{${placeholder}}`)
          .join('、')}`,
      );
      return;
    }
    setSaving(true);
    setError(null);
    try {
      applyPrompt(await promptApi.saveDraftPrompt(content));
      message.success('初稿提示词已保存');
    } catch (cause) {
      setError(promptErrorMessage(cause, '保存初稿提示词失败'));
    } finally {
      setSaving(false);
    }
  };

  const reset = async () => {
    setResetting(true);
    setError(null);
    try {
      applyPrompt(await promptApi.resetDraftPrompt());
      message.success('已恢复系统默认提示词');
    } catch (cause) {
      setError(promptErrorMessage(cause, '恢复系统默认提示词失败'));
    } finally {
      setResetting(false);
    }
  };

  if (loading) return <Skeleton active paragraph={{ rows: 12 }} />;

  if (!prompt) {
    return (
      <Alert
        showIcon
        type="error"
        message="提示词加载失败"
        description={error}
        action={<Button onClick={load}>重新加载</Button>}
      />
    );
  }

  return (
    <Card
      title="初稿生成提示词"
      extra={
        <Tag color={prompt.is_custom ? 'green' : 'default'}>
          {prompt.is_custom ? '已自定义' : '系统默认'}
        </Tag>
      }
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Paragraph type="secondary" style={{ margin: 0 }}>
          此 System Prompt 会应用于初稿生成和重写。未保存的修改不会影响正在运行的任务。
        </Paragraph>
        <Alert
          showIcon
          type="info"
          message="必需占位符"
          description={
            <Space size={[4, 4]} wrap>
              {prompt.required_placeholders.map((placeholder) => (
                <Tag key={placeholder}>{`{${placeholder}}`}</Tag>
              ))}
              <Text type="secondary">保存时必须完整保留，运行时会替换为对应内容。</Text>
            </Space>
          }
        />
        {error && (
          <Alert
            showIcon
            closable
            type="error"
            message="提示词操作失败"
            description={error}
            onClose={() => setError(null)}
          />
        )}
        <TextArea
          aria-label="初稿生成提示词内容"
          value={content}
          maxLength={20000}
          showCount
          autoSize={{ minRows: 18, maxRows: 32 }}
          style={{ fontFamily: 'SFMono-Regular, Consolas, Liberation Mono, monospace' }}
          onChange={(event) => setContent(event.target.value)}
        />
        <Space>
          <Button
            type="primary"
            icon={<SaveOutlined />}
            loading={saving}
            disabled={!dirty}
            onClick={save}
          >
            保存修改
          </Button>
          <Popconfirm
            title="恢复系统默认？"
            description="将删除您的自定义提示词，确认恢复？"
            okText="确认恢复"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={reset}
          >
            <Button icon={<ReloadOutlined />} loading={resetting} disabled={!prompt.is_custom}>
              恢复系统默认
            </Button>
          </Popconfirm>
          {dirty && <Text type="warning">有未保存的修改</Text>}
        </Space>
      </Space>
    </Card>
  );
}
