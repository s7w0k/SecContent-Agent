import { EyeOutlined, SaveOutlined } from '@ant-design/icons';
import { Alert, Button, Divider, Form, Input, Modal, Space, Typography, message } from 'antd';
import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { prTemplateApi } from '../api/client';
import type { EffectivePRTemplate, PRTemplateUpdate } from '../types';
import { templateErrorMessage } from '../utils/templateErrors';
import SectionEditor from './SectionEditor';

const { Text } = Typography;

interface TemplateEditorProps {
  template: EffectivePRTemplate;
  onSaved: (template: EffectivePRTemplate) => void;
  onDirtyChange: (dirty: boolean) => void;
}

function formValues(template: EffectivePRTemplate): PRTemplateUpdate {
  return {
    name: template.name,
    title_template: template.title_template,
    sections: template.sections.map((section, index) => ({ ...section, order: index + 1 })),
    perspectives: [template.perspectives[0], template.perspectives[1]],
    extra_instructions: template.extra_instructions,
    expected_version: template.version,
  };
}

function normalized(values: PRTemplateUpdate, version: number): PRTemplateUpdate {
  return {
    ...values,
    name: values.name.trim(),
    title_template: values.title_template.trim(),
    sections: values.sections.map((section, index) => ({
      heading: section.heading.trim(),
      guide: section.guide.trim(),
      order: index + 1,
    })),
    perspectives: [values.perspectives[0].trim(), values.perspectives[1].trim()],
    extra_instructions: values.extra_instructions.trim(),
    expected_version: version,
  };
}

export default function TemplateEditor({ template, onSaved, onDirtyChange }: TemplateEditorProps) {
  const [form] = Form.useForm<PRTemplateUpdate>();
  const [saving, setSaving] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [preview, setPreview] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    form.setFieldsValue(formValues(template));
    setError(null);
    setPreview(null);
    onDirtyChange(false);
  }, [form, onDirtyChange, template]);

  const values = async () => normalized(await form.validateFields(), template.version);

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      const saved = await prTemplateApi.save(template.template_key, await values());
      form.setFieldsValue(formValues(saved));
      onDirtyChange(false);
      onSaved(saved);
      message.success('模板已保存');
    } catch (cause) {
      setError(templateErrorMessage(cause, '保存模板失败'));
    } finally {
      setSaving(false);
    }
  };

  const handlePreview = async () => {
    setPreviewing(true);
    setError(null);
    try {
      setPreview(await prTemplateApi.preview(template.template_key, await values()));
    } catch (cause) {
      setError(templateErrorMessage(cause, '预览模板失败'));
    } finally {
      setPreviewing(false);
    }
  };

  return (
    <>
      {error && (
        <Alert showIcon closable type="error" message={error} onClose={() => setError(null)} />
      )}
      <Form
        form={form}
        layout="vertical"
        initialValues={formValues(template)}
        onValuesChange={() => onDirtyChange(true)}
        style={{ marginTop: 16 }}
      >
        <Form.Item
          label="模板名称"
          name="name"
          rules={[
            { required: true, whitespace: true, message: '请输入模板名称' },
            { max: 100, message: '模板名称不能超过 100 个字符' },
          ]}
        >
          <Input placeholder="用于模板卡片和偏好统计展示" />
        </Form.Item>
        <Form.Item
          label="标题骨架"
          name="title_template"
          rules={[
            { required: true, whitespace: true, message: '请输入标题骨架' },
            { max: 300, message: '标题骨架不能超过 300 个字符' },
          ]}
        >
          <Input.TextArea rows={2} placeholder="# [事件名称]：影响分析" />
        </Form.Item>

        <Divider orientation="left">正文章节</Divider>
        <SectionEditor />

        <Divider orientation="left">生成视角</Divider>
        <Space direction="vertical" size={0} style={{ width: '100%' }}>
          <Text type="secondary">每套模板固定提供两个生成视角。</Text>
          <Space align="start" style={{ width: '100%', marginTop: 8 }}>
            <Form.Item
              label="视角一"
              name={['perspectives', 0]}
              rules={[{ required: true, whitespace: true, message: '请输入第一个视角' }]}
              style={{ minWidth: 280 }}
            >
              <Input maxLength={200} />
            </Form.Item>
            <Form.Item
              label="视角二"
              name={['perspectives', 1]}
              dependencies={[['perspectives', 0]]}
              rules={[
                { required: true, whitespace: true, message: '请输入第二个视角' },
                ({ getFieldValue }) => ({
                  validator(_, value) {
                    return value && value.trim() === getFieldValue(['perspectives', 0])?.trim()
                      ? Promise.reject(new Error('两个视角不能相同'))
                      : Promise.resolve();
                  },
                }),
              ]}
              style={{ minWidth: 280 }}
            >
              <Input maxLength={200} />
            </Form.Item>
          </Space>
        </Space>

        <Form.Item
          label="补充要求"
          name="extra_instructions"
          rules={[{ max: 2000, message: '补充要求不能超过 2000 个字符' }]}
        >
          <Input.TextArea rows={4} showCount maxLength={2000} />
        </Form.Item>

        <Space>
          <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={handleSave}>
            保存模板
          </Button>
          <Button icon={<EyeOutlined />} loading={previewing} onClick={handlePreview}>
            预览骨架
          </Button>
        </Space>
      </Form>

      <Modal
        title="模板骨架预览"
        open={preview !== null}
        onCancel={() => setPreview(null)}
        footer={<Button onClick={() => setPreview(null)}>关闭</Button>}
        width={760}
      >
        <div style={{ maxHeight: '60vh', overflow: 'auto', padding: '8px 16px' }}>
          <ReactMarkdown>{preview || ''}</ReactMarkdown>
        </div>
      </Modal>
    </>
  );
}
