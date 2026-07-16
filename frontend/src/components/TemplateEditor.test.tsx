import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Button, Form } from 'antd';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { prTemplateApi } from '../api/client';
import type { EffectivePRTemplate } from '../types';
import SectionEditor from './SectionEditor';
import TemplateEditor from './TemplateEditor';

vi.mock('../api/client', () => ({
  prTemplateApi: {
    save: vi.fn(),
    preview: vi.fn(),
  },
}));

const template: EffectivePRTemplate = {
  template_id: 'tpl-breaking-a',
  template_key: 'breaking_a',
  category_v2: '爆点事件',
  slot: 'A',
  source: 'user',
  version: 3,
  system_version: 1,
  name: '用户爆点模板',
  title_template: '# [事件名称]：安全分析',
  sections: [
    { heading: '事件概述', guide: '描述事件背景', order: 1 },
    { heading: '安全影响', guide: '分析身份安全影响', order: 2 },
  ],
  perspectives: ['技术视角', '市场视角'],
  extra_instructions: '突出智能体身份安全',
  updated_at: '2026-07-16T08:00:00Z',
};

describe('TemplateEditor', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(prTemplateApi.save).mockResolvedValue({ ...template, version: 4 });
    vi.mocked(prTemplateApi.preview).mockResolvedValue('# 预览');
  });

  it('validates required fields and distinct perspectives before saving', async () => {
    render(<TemplateEditor template={template} onSaved={vi.fn()} onDirtyChange={vi.fn()} />);

    fireEvent.change(screen.getByLabelText('模板名称'), { target: { value: '   ' } });
    fireEvent.change(screen.getByLabelText('视角二'), { target: { value: '技术视角' } });
    fireEvent.click(screen.getByRole('button', { name: /保存模板/ }));

    expect(await screen.findByText('请输入模板名称')).toBeInTheDocument();
    expect(await screen.findByText('两个视角不能相同')).toBeInTheDocument();
    expect(prTemplateApi.save).not.toHaveBeenCalled();
  });

  it('normalizes editable content and sends the optimistic version', async () => {
    const onSaved = vi.fn();
    const onDirtyChange = vi.fn();
    render(<TemplateEditor template={template} onSaved={onSaved} onDirtyChange={onDirtyChange} />);

    fireEvent.change(screen.getByLabelText('模板名称'), {
      target: { value: '  新模板名称  ' },
    });
    fireEvent.change(screen.getByLabelText('补充要求'), {
      target: { value: '  只引用原文事实  ' },
    });
    fireEvent.click(screen.getByRole('button', { name: /保存模板/ }));

    await waitFor(() => expect(prTemplateApi.save).toHaveBeenCalledTimes(1));
    expect(prTemplateApi.save).toHaveBeenCalledWith(
      'breaking_a',
      expect.objectContaining({
        name: '新模板名称',
        extra_instructions: '只引用原文事实',
        expected_version: 3,
      }),
    );
    expect(onDirtyChange).toHaveBeenCalledWith(true);
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
    expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ version: 4 }));
  });

  it('shows a friendly message for optimistic-lock conflicts', async () => {
    vi.mocked(prTemplateApi.save).mockRejectedValue({ response: { status: 409 } });
    render(<TemplateEditor template={template} onSaved={vi.fn()} onDirtyChange={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: /保存模板/ }));

    expect(await screen.findByText('模板已被其他会话修改，请刷新模板后重试。')).toBeInTheDocument();
  });
});

describe('SectionEditor', () => {
  function renderSections(onFinish = vi.fn()) {
    return {
      onFinish,
      ...render(
        <Form initialValues={{ sections: template.sections }} onFinish={onFinish} layout="vertical">
          <SectionEditor />
          <Button htmlType="submit">提交章节</Button>
        </Form>,
      ),
    };
  }

  it('adds and deletes sections while preserving at least one section', () => {
    renderSections();

    fireEvent.click(screen.getByRole('button', { name: /添加章节/ }));
    expect(screen.getAllByLabelText('章节标题')).toHaveLength(3);
    fireEvent.click(screen.getByRole('button', { name: '删除章节 3' }));
    expect(screen.getAllByLabelText('章节标题')).toHaveLength(2);

    fireEvent.click(screen.getByRole('button', { name: '删除章节 2' }));
    expect(screen.getAllByLabelText('章节标题')).toHaveLength(1);
    expect(screen.getByRole('button', { name: '删除章节 1' })).toBeDisabled();
  });

  it('reorders sections with controls and native drag events', () => {
    const { container } = renderSections();
    const headings = () => screen.getAllByLabelText('章节标题') as HTMLInputElement[];

    fireEvent.click(screen.getByRole('button', { name: '下移章节 1' }));
    expect(headings()[0]).toHaveValue('安全影响');

    const cards = container.querySelectorAll('.ant-card[draggable="true"]');
    fireEvent.dragStart(cards[0]);
    fireEvent.dragOver(cards[1]);
    fireEvent.drop(cards[1]);
    expect(headings()[0]).toHaveValue('事件概述');
  });
});
