import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { knowledgeApi } from '../api/client';
import type {
  KnowledgeDocument,
  KnowledgeSearchResult,
  KnowledgeStatus,
  KnowledgeTree,
  KnowledgeUsageItem,
} from '../types';
import ProductKnowledgePage from './ProductKnowledgePage';

// Mock react-markdown（KnowledgeViewer 内部使用）
vi.mock('react-markdown', () => ({
  default: ({ children }: { children: string }) => <div data-testid="markdown">{children}</div>,
}));

vi.mock('../api/client', () => ({
  knowledgeApi: {
    getTree: vi.fn(),
    getDocument: vi.fn(),
    search: vi.fn(),
    getStatus: vi.fn(),
    getUsageMap: vi.fn(),
  },
}));

const mockTree: KnowledgeTree = {
  root_name: 'knowledge',
  knowledge_hash: 'abc123def456',
  loaded_at: '2026-07-27T08:00:00Z',
  children: [
    {
      name: 'docs',
      path: 'docs',
      node_type: 'dir',
      children: [
        {
          name: 'deep_doc.md',
          path: 'docs/deep_doc.md',
          node_type: 'file',
        },
      ],
    },
    {
      name: 'intro.md',
      path: 'intro.md',
      node_type: 'file',
    },
    {
      name: 'guide.md',
      path: 'guide.md',
      node_type: 'file',
    },
    {
      name: 'README.md',
      path: 'README.md',
      node_type: 'file',
    },
  ],
};

const mockStatus: KnowledgeStatus = {
  root_path: '/app/documents',
  loaded: true,
  file_count: 42,
  loader_relevant_count: 12,
  direct_scoring_file_count: 5,
  knowledge_hash: 'abc123def456',
  loaded_at: '2026-07-27T08:00:00Z',
};

const mockUsageMap: KnowledgeUsageItem[] = [
  { role: 'product_fact', label: '产品事实', description: '产品相关事实信息' },
  { role: 'market_brief', label: '市场简报', description: '市场动态简报' },
];

const mockDocument: KnowledgeDocument = {
  relative_path: 'docs/intro.md',
  content: '# 简介\n\n这是知识库的介绍文档。',
  content_hash: 'hash123abc',
  size: 128,
  updated_at: '2026-07-26T10:00:00Z',
  document_id: 'docs/intro.md',
  name: 'intro.md',
  knowledge_role: 'product_fact',
  loader_relevant: true,
  direct_scoring_prompt: false,
  editable: true,
  protected_path: false,
};

const mockSearchResults: KnowledgeSearchResult[] = [
  {
    relative_path: 'docs/guide.md',
    name: 'guide.md',
    content_hash: 'hash456',
    size: 256,
    snippet: '这是一段包含关键词的片段',
    knowledge_role: 'market_brief',
    direct_scoring_prompt: true,
    document_id: 'docs/guide.md',
  },
];

describe('ProductKnowledgePage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(knowledgeApi.getTree).mockResolvedValue(mockTree);
    vi.mocked(knowledgeApi.getStatus).mockResolvedValue(mockStatus);
    vi.mocked(knowledgeApi.getUsageMap).mockResolvedValue(mockUsageMap);
    vi.mocked(knowledgeApi.getDocument).mockResolvedValue(mockDocument);
    vi.mocked(knowledgeApi.search).mockResolvedValue(mockSearchResults);
  });

  it('renders the page title and description', async () => {
    render(<ProductKnowledgePage />);

    expect(await screen.findByText('产品知识库')).toBeInTheDocument();
    expect(screen.getByText('浏览产品知识库的正式文档和目录结构')).toBeInTheDocument();
  });

  it('shows the status card with loaded info', async () => {
    render(<ProductKnowledgePage />);

    await screen.findByText('产品知识库');
    expect(await screen.findByText('42')).toBeInTheDocument();
    expect(screen.getByText('12')).toBeInTheDocument();
    expect(screen.getByText('5')).toBeInTheDocument();
    expect(screen.getByText('abc123de')).toBeInTheDocument();
    expect(screen.getByText('已加载')).toBeInTheDocument();
  });

  it('renders the tree with file nodes', async () => {
    render(<ProductKnowledgePage />);

    expect(await screen.findByText('intro.md')).toBeInTheDocument();
    expect(screen.getByText('guide.md')).toBeInTheDocument();
    expect(screen.getByText('README.md')).toBeInTheDocument();
  });

  it('loads document content when a file is clicked', async () => {
    render(<ProductKnowledgePage />);

    const fileNode = await screen.findByText('intro.md');
    fireEvent.click(fileNode);

    await waitFor(() => expect(knowledgeApi.getDocument).toHaveBeenCalled());
    expect((await screen.findAllByText(/简介/)).length).toBeGreaterThan(0);
    expect((await screen.findAllByText(/这是知识库的介绍文档/)).length).toBeGreaterThan(0);
  });

  it('shows document metadata after loading', async () => {
    render(<ProductKnowledgePage />);

    fireEvent.click(await screen.findByText('intro.md'));

    await waitFor(() => expect(knowledgeApi.getDocument).toHaveBeenCalled());
    // 产品事实 appears both in the usage legend and the document badge
    const roleBadges = await screen.findAllByText('产品事实');
    expect(roleBadges.length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText('评分相关')).toBeInTheDocument();
  });

  it('searches and displays search results', async () => {
    render(<ProductKnowledgePage />);

    await screen.findByText('intro.md');

    const searchInput = screen.getByPlaceholderText('搜索文件名或内容');
    fireEvent.change(searchInput, { target: { value: '关键词' } });
    fireEvent.click(screen.getByRole('button', { name: 'search' }));

    await waitFor(() => expect(knowledgeApi.search).toHaveBeenCalledWith('关键词'));
    expect(await screen.findByText('guide.md')).toBeInTheDocument();
    expect(screen.getByText('核心打分')).toBeInTheDocument();
    expect(screen.getByText('这是一段包含关键词的片段')).toBeInTheDocument();
  });

  it('loads document when a search result is clicked', async () => {
    render(<ProductKnowledgePage />);

    await screen.findByText('intro.md');

    const searchInput = screen.getByPlaceholderText('搜索文件名或内容');
    fireEvent.change(searchInput, { target: { value: '关键词' } });
    fireEvent.click(screen.getByRole('button', { name: 'search' }));

    const resultItem = await screen.findByText('guide.md');
    fireEvent.click(resultItem);

    await waitFor(() =>
      expect(knowledgeApi.getDocument).toHaveBeenCalledWith(
        'docs/guide.md',
      ),
    );
  });

  it('renders usage legend items', async () => {
    render(<ProductKnowledgePage />);

    expect(await screen.findAllByText('产品事实')).toHaveLength(1);
  });
});
