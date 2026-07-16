import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { prTemplateApi } from '../api/client';
import type { EffectivePRTemplate, PRTemplateCategory, PRTemplateKey } from '../types';
import PRTemplatesPage from './PRTemplatesPage';

vi.mock('../api/client', () => ({
  prTemplateApi: {
    list: vi.fn(),
    save: vi.fn(),
    preview: vi.fn(),
    reset: vi.fn(),
    versions: vi.fn(),
    restore: vi.fn(),
  },
}));

function template(
  templateKey: PRTemplateKey,
  category: PRTemplateCategory,
  slot: 'A' | 'B',
  name: string,
  source: 'system' | 'user' = 'system',
): EffectivePRTemplate {
  return {
    template_id: source === 'user' ? `tpl-${templateKey}` : `system:${templateKey}`,
    template_key: templateKey,
    category_v2: category,
    slot,
    source,
    version: source === 'user' ? 3 : 1,
    system_version: 1,
    name,
    title_template: '# [事件名称]：安全影响分析',
    sections: [
      { heading: '事件概述', guide: '描述事件背景', order: 1 },
      { heading: '安全影响', guide: '分析身份安全影响', order: 2 },
    ],
    perspectives: ['技术视角', '市场视角'],
    extra_instructions: '',
    updated_at: source === 'user' ? '2026-07-16T08:00:00Z' : null,
  };
}

const templates = [
  template('breaking_a', '爆点事件', 'A', '用户爆点 A', 'user'),
  template('breaking_b', '爆点事件', 'B', '系统爆点 B'),
  template('law_a', '法律法规/监管动态', 'A', '法规 A'),
  template('law_b', '法律法规/监管动态', 'B', '法规 B'),
  template('ai_a', 'AI技术重大进展', 'A', 'AI 技术 A'),
  template('ai_b', 'AI技术重大进展', 'B', 'AI 技术 B'),
];

describe('PRTemplatesPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(prTemplateApi.list).mockResolvedValue({ items: templates, total: 6 });
    vi.mocked(prTemplateApi.save).mockImplementation(async (_key, payload) => ({
      ...templates[0],
      ...payload,
      version: 4,
      source: 'user',
    }));
    vi.mocked(prTemplateApi.preview).mockResolvedValue('# 模板骨架预览');
    vi.mocked(prTemplateApi.versions).mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      page_size: 10,
    });
    vi.mocked(prTemplateApi.reset).mockResolvedValue({
      ...templates[0],
      template_id: 'system:breaking_a',
      name: '系统爆点 A',
      source: 'system',
      version: 1,
      updated_at: null,
    });
    vi.mocked(prTemplateApi.restore).mockResolvedValue({
      ...templates[0],
      name: '历史爆点模板',
      version: 4,
    });
  });

  it('loads six templates and switches category cards', async () => {
    render(<PRTemplatesPage />);

    expect(await screen.findByText('用户爆点 A')).toBeDefined();
    expect(screen.getByText('用户自定义')).toBeDefined();
    expect(screen.getByText('系统爆点 B')).toBeDefined();

    fireEvent.click(screen.getByRole('tab', { name: 'AI 技术重大进展' }));

    expect(await screen.findByText('AI 技术 A')).toBeDefined();
    expect(screen.queryByText('用户爆点 A')).not.toBeInTheDocument();
  });

  it('edits, validates and saves a tenant template with optimistic version', async () => {
    const onDirtyChange = vi.fn();
    render(<PRTemplatesPage onDirtyChange={onDirtyChange} />);

    await screen.findByText('用户爆点 A');
    fireEvent.click(screen.getAllByRole('button', { name: /编辑/ })[0]);
    const nameInput = await screen.findByLabelText('模板名称');
    fireEvent.change(nameInput, { target: { value: '新的爆点模板' } });
    fireEvent.click(screen.getByRole('button', { name: /保存模板/ }));

    await waitFor(() => expect(prTemplateApi.save).toHaveBeenCalledTimes(1));
    expect(prTemplateApi.save).toHaveBeenCalledWith(
      'breaking_a',
      expect.objectContaining({ name: '新的爆点模板', expected_version: 3 }),
    );
    expect(onDirtyChange).toHaveBeenCalledWith(true);
    expect(await screen.findByText('新的爆点模板')).toBeDefined();
  });

  it('previews the form through the non-LLM preview endpoint', async () => {
    render(<PRTemplatesPage />);

    await screen.findByText('用户爆点 A');
    fireEvent.click(screen.getAllByRole('button', { name: /编辑/ })[0]);
    fireEvent.click(await screen.findByRole('button', { name: /预览骨架/ }));

    await waitFor(() => expect(prTemplateApi.preview).toHaveBeenCalledTimes(1));
    expect((await screen.findAllByText('模板骨架预览')).length).toBeGreaterThan(1);
  });

  it('resets only the selected user template after confirmation', async () => {
    render(<PRTemplatesPage />);

    await screen.findByText('用户爆点 A');
    fireEvent.click(screen.getAllByRole('button', { name: /恢复默认/ })[0]);
    fireEvent.click(await screen.findByRole('button', { name: '恢复默认' }));

    await waitFor(() => expect(prTemplateApi.reset).toHaveBeenCalledWith('breaking_a'));
    expect(await screen.findByText('系统爆点 A')).toBeInTheDocument();
    expect(screen.queryByText('用户爆点 A')).not.toBeInTheDocument();
  });

  it('loads history and restores a snapshot as a new version', async () => {
    vi.mocked(prTemplateApi.versions).mockResolvedValue({
      items: [
        {
          version_id: 'version-1',
          template_id: 'tpl-breaking-a',
          template_key: 'breaking_a',
          version: 1,
          change_type: 'create',
          created_at: '2026-07-15T08:00:00Z',
          snapshot: {
            template_key: 'breaking_a',
            category_v2: '爆点事件',
            slot: 'A',
            name: '历史爆点模板',
            title_template: '# 历史标题',
            sections: [{ heading: '历史章节', guide: '历史指引', order: 1 }],
            perspectives: ['技术视角', '市场视角'],
            extra_instructions: '',
          },
        },
      ],
      total: 1,
      page: 1,
      page_size: 10,
    });
    render(<PRTemplatesPage />);

    await screen.findByText('用户爆点 A');
    fireEvent.click(screen.getAllByRole('button', { name: /历史/ })[0]);
    expect(await screen.findByText('历史爆点模板')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /恢复此版本/ }));
    fireEvent.click(await screen.findByRole('button', { name: '确认恢复' }));

    await waitFor(() => expect(prTemplateApi.restore).toHaveBeenCalledWith('breaking_a', 1));
    expect((await screen.findAllByText('v4')).length).toBeGreaterThan(0);
  });

  it('does not close the editor without warning when changes are unsaved', async () => {
    render(<PRTemplatesPage />);

    await screen.findByText('用户爆点 A');
    fireEvent.click(screen.getAllByRole('button', { name: /编辑/ })[0]);
    fireEvent.change(await screen.findByLabelText('模板名称'), {
      target: { value: '未保存模板' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));

    expect((await screen.findAllByText('放弃未保存的修改？')).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('button', { name: '继续编辑' }));
    expect(screen.getByLabelText('模板名称')).toHaveValue('未保存模板');
  });

  it('renders list API failures without exposing an empty success state', async () => {
    vi.mocked(prTemplateApi.list).mockRejectedValue(new Error('network unavailable'));
    render(<PRTemplatesPage />);

    expect(await screen.findByText('模板操作失败')).toBeInTheDocument();
    expect(screen.getByText('network unavailable')).toBeInTheDocument();
  });
});
