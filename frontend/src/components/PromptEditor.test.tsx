import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { promptApi } from '../api/client';
import type { EffectivePrompt } from '../types';
import PromptEditor from './PromptEditor';

vi.mock('../api/client', () => ({
  promptApi: {
    getDraftPrompt: vi.fn(),
    saveDraftPrompt: vi.fn(),
    resetDraftPrompt: vi.fn(),
  },
}));

const DEFAULT_CONTENT = [
  '知识：{knowledge_context}',
  '模板：{template_spec}',
  '风格：{style_hints}',
].join('\n');

const defaultPrompt: EffectivePrompt = {
  prompt_key: 'draft_system',
  content: DEFAULT_CONTENT,
  is_custom: false,
  required_placeholders: ['knowledge_context', 'template_spec', 'style_hints'],
  updated_at: null,
};

describe('PromptEditor', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(promptApi.getDraftPrompt).mockResolvedValue(defaultPrompt);
  });

  it('loads the system prompt and shows its placeholders', async () => {
    render(<PromptEditor />);

    expect(await screen.findByLabelText('初稿生成提示词内容')).toHaveValue(DEFAULT_CONTENT);
    expect(screen.getByText('系统默认')).toBeInTheDocument();
    expect(screen.getByText('{knowledge_context}')).toBeInTheDocument();
    expect(screen.getByText('{template_spec}')).toBeInTheDocument();
    expect(screen.getByText('{style_hints}')).toBeInTheDocument();
  });

  it('blocks saving when a required placeholder is missing', async () => {
    render(<PromptEditor />);
    const editor = await screen.findByLabelText('初稿生成提示词内容');

    fireEvent.change(editor, {
      target: { value: '知识：{knowledge_context}\n模板：{template_spec}\n风格已删除' },
    });
    fireEvent.click(screen.getByRole('button', { name: /保存修改/ }));

    expect(await screen.findByText(/请保留以下必需占位符：\{style_hints\}/)).toBeInTheDocument();
    expect(promptApi.saveDraftPrompt).not.toHaveBeenCalled();
  });

  it('saves valid content and clears the dirty state', async () => {
    const onDirtyChange = vi.fn();
    const customContent = `${DEFAULT_CONTENT}\n请使用简洁语气。`;
    vi.mocked(promptApi.saveDraftPrompt).mockResolvedValue({
      ...defaultPrompt,
      content: customContent,
      is_custom: true,
      updated_at: '2026-07-22T03:00:00Z',
    });
    render(<PromptEditor onDirtyChange={onDirtyChange} />);
    const editor = await screen.findByLabelText('初稿生成提示词内容');

    fireEvent.change(editor, { target: { value: customContent } });
    await waitFor(() => expect(onDirtyChange).toHaveBeenCalledWith(true));
    fireEvent.click(screen.getByRole('button', { name: /保存修改/ }));

    await waitFor(() => expect(promptApi.saveDraftPrompt).toHaveBeenCalledWith(customContent));
    expect(await screen.findByText('已自定义')).toBeInTheDocument();
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(false));
  });

  it('resets a custom prompt after confirmation', async () => {
    const customPrompt = {
      ...defaultPrompt,
      content: `${DEFAULT_CONTENT}\n自定义`,
      is_custom: true,
    };
    vi.mocked(promptApi.getDraftPrompt).mockResolvedValue(customPrompt);
    vi.mocked(promptApi.resetDraftPrompt).mockResolvedValue(defaultPrompt);
    render(<PromptEditor />);

    fireEvent.click(await screen.findByRole('button', { name: /恢复系统默认/ }));
    expect(await screen.findByText('将删除您的自定义提示词，确认恢复？')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '确认恢复' }));

    await waitFor(() => expect(promptApi.resetDraftPrompt).toHaveBeenCalledOnce());
    expect(await screen.findByText('系统默认')).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByLabelText('初稿生成提示词内容')).toHaveValue(DEFAULT_CONTENT),
    );
  });
});
