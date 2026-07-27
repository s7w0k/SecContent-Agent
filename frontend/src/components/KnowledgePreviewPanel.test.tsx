import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { knowledgeAdminApi } from '../api/client';
import type {
  KnowledgePromptPreview,
  KnowledgeScorePreview,
  KnowledgeValidationResult,
} from '../types';
import KnowledgePreviewPanel from './KnowledgePreviewPanel';

vi.mock('../api/client', () => ({
  knowledgeAdminApi: {
    validateDraft: vi.fn(),
    previewPrompt: vi.fn(),
    previewScore: vi.fn(),
  },
}));

const mockValidationPassed: KnowledgeValidationResult = {
  status: 'passed',
  errors: [],
  warnings: [],
  loader_file_count: 42,
  loader_relevant_count: 12,
};

const mockValidationFailed: KnowledgeValidationResult = {
  status: 'failed',
  errors: ['文件格式错误：缺少必要字段'],
  warnings: ['README.md 未被加载器引用'],
  loader_file_count: 3,
  loader_relevant_count: 1,
};

const mockPromptPreview: KnowledgePromptPreview = {
  old_prompt: '旧 Prompt 内容\n评分规则：A',
  new_prompt: '新 Prompt 内容\n评分规则：B',
  old_hash: 'abc123def456abc123def456abc123de',
  new_hash: 'f78901234567f78901234567f78901234',
  prompt_changed: true,
  file_in_prompt: true,
  char_count_old: 100,
  char_count_new: 150,
};

const mockScorePreview: KnowledgeScorePreview = {
  old_score: {
    product_relevance: 3,
    event_impact: 4,
    pr_total_score: 12,
  },
  new_score: {
    product_relevance: 5,
    event_impact: 5,
    pr_total_score: 25,
  },
  score_changed: true,
};

describe('KnowledgePreviewPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(knowledgeAdminApi.validateDraft).mockResolvedValue(mockValidationPassed);
    vi.mocked(knowledgeAdminApi.previewPrompt).mockResolvedValue(mockPromptPreview);
    vi.mocked(knowledgeAdminApi.previewScore).mockResolvedValue(mockScorePreview);
  });

  it('renders 3 tabs', () => {
    render(<KnowledgePreviewPanel draftId="draft-1" relativePath="docs/intro.md" />);

    expect(screen.getByRole('tab', { name: '校验' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Prompt 预览' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '试打分' })).toBeInTheDocument();
  });

  it('shows the associated relative path', () => {
    render(<KnowledgePreviewPanel draftId="draft-1" relativePath="docs/intro.md" />);

    expect(screen.getByText('docs/intro.md')).toBeInTheDocument();
  });

  it('calls validateDraft and shows results when clicking validate', async () => {
    render(<KnowledgePreviewPanel draftId="draft-1" relativePath="docs/intro.md" />);

    fireEvent.click(screen.getByRole('button', { name: /运行校验/ }));

    await waitFor(() => expect(knowledgeAdminApi.validateDraft).toHaveBeenCalledWith('draft-1'));
    // "校验通过" 同时出现在 message 通知和结果 Tag 中
    expect((await screen.findAllByText('校验通过')).length).toBeGreaterThan(0);
    expect(screen.getByText('加载文件数')).toBeInTheDocument();
    expect(screen.getByText('42')).toBeInTheDocument();
    expect(screen.getByText('评分相关文件数')).toBeInTheDocument();
    expect(screen.getByText('12')).toBeInTheDocument();
  });

  it('shows errors and warnings when validation fails', async () => {
    vi.mocked(knowledgeAdminApi.validateDraft).mockResolvedValue(mockValidationFailed);

    render(<KnowledgePreviewPanel draftId="draft-1" relativePath="docs/intro.md" />);

    fireEvent.click(screen.getByRole('button', { name: /运行校验/ }));

    expect(await screen.findByText('校验失败')).toBeInTheDocument();
    expect(screen.getByText('文件格式错误：缺少必要字段')).toBeInTheDocument();
    expect(screen.getByText('README.md 未被加载器引用')).toBeInTheDocument();
  });

  it('shows hash comparison and prompt diff on prompt preview tab', async () => {
    render(<KnowledgePreviewPanel draftId="draft-1" relativePath="docs/intro.md" />);

    // 切换到 Prompt 预览标签页
    fireEvent.click(screen.getByRole('tab', { name: 'Prompt 预览' }));

    fireEvent.click(screen.getByRole('button', { name: /生成 Prompt 预览/ }));

    await waitFor(() => expect(knowledgeAdminApi.previewPrompt).toHaveBeenCalledWith('draft-1'));
    expect(await screen.findByText('Prompt 已变化')).toBeInTheDocument();
    expect(screen.getByText('当前文件在 Prompt 中')).toBeInTheDocument();
    // 旧哈希前 12 位
    expect(screen.getByText('abc123def456')).toBeInTheDocument();
    // 新哈希前 12 位
    expect(screen.getByText('f78901234567')).toBeInTheDocument();
    // 字符数统计
    expect(screen.getByText('100')).toBeInTheDocument();
    expect(screen.getByText('150')).toBeInTheDocument();
    // Prompt 内容并排展示
    expect(screen.getByLabelText('旧 Prompt 内容')).toHaveTextContent('旧 Prompt 内容');
    expect(screen.getByLabelText('新 Prompt 内容')).toHaveTextContent('新 Prompt 内容');
  });

  it('shows LLM cost warning on trial score tab', () => {
    render(<KnowledgePreviewPanel draftId="draft-1" relativePath="docs/intro.md" />);

    fireEvent.click(screen.getByRole('tab', { name: '试打分' }));

    expect(screen.getByText('试打分会调用 LLM，产生模型费用')).toBeInTheDocument();
  });

  it('disables trial score button when article title or content is empty', () => {
    render(<KnowledgePreviewPanel draftId="draft-1" relativePath="docs/intro.md" />);

    fireEvent.click(screen.getByRole('tab', { name: '试打分' }));

    const runButton = screen.getByRole('button', { name: /运行试打分/ });
    expect(runButton).toBeDisabled();
  });

  it('enables trial score button and runs scoring when required fields are filled', async () => {
    render(<KnowledgePreviewPanel draftId="draft-1" relativePath="docs/intro.md" />);

    fireEvent.click(screen.getByRole('tab', { name: '试打分' }));

    const titleInput = screen.getByLabelText('文章标题');
    const contentInput = screen.getByLabelText('文章正文');

    fireEvent.change(titleInput, { target: { value: '测试标题' } });
    fireEvent.change(contentInput, { target: { value: '测试正文内容' } });

    const runButton = screen.getByRole('button', { name: /运行试打分/ });
    expect(runButton).not.toBeDisabled();

    fireEvent.click(runButton);

    await waitFor(() => expect(knowledgeAdminApi.previewScore).toHaveBeenCalled());
    expect(knowledgeAdminApi.previewScore).toHaveBeenCalledWith('draft-1', {
      title: '测试标题',
      source: '',
      category_v2: '',
      summary_cn: '',
      content_md: '测试正文内容',
    });
    // 分数对比
    expect(await screen.findByText('分数已变化')).toBeInTheDocument();
    expect(screen.getAllByText('产品相关性')).toHaveLength(2);
    expect(screen.getAllByText('事件影响力')).toHaveLength(2);
    expect(screen.getAllByText('PR 总分')).toHaveLength(2);
  });

  it('shows error message when API call fails', async () => {
    vi.mocked(knowledgeAdminApi.validateDraft).mockRejectedValue({
      response: { data: { detail: '草稿不存在' } },
    });

    render(<KnowledgePreviewPanel draftId="bad-draft" relativePath="docs/intro.md" />);

    fireEvent.click(screen.getByRole('button', { name: /运行校验/ }));

    await waitFor(() => expect(knowledgeAdminApi.validateDraft).toHaveBeenCalled());
    expect(await screen.findByText('草稿不存在')).toBeInTheDocument();
  });
});
