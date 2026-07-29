import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import api from '../api/client';
import type { SearchStatusResponse } from '../types';
import WebSearchPage from './WebSearchPage';

vi.mock('../api/client', () => ({
  default: {
    webSearchApi: {
      getStatus: vi.fn(),
      search: vi.fn(),
      getSession: vi.fn(),
      importResults: vi.fn(),
    },
  },
}));

const enabledStatus: SearchStatusResponse = {
  enabled: true,
  available: true,
  allowed_categories: ['general', 'news'],
  allowed_languages: ['zh-CN', 'en'],
  max_import_items: 20,
};

const disabledStatus: SearchStatusResponse = {
  enabled: false,
  available: false,
  allowed_categories: [],
  allowed_languages: [],
  max_import_items: 20,
};

describe('WebSearchPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders search form when status is enabled', async () => {
    vi.mocked(api.webSearchApi.getStatus).mockResolvedValue(enabledStatus);
    render(<WebSearchPage onNavigate={vi.fn()} />);

    expect(await screen.findByPlaceholderText('输入搜索关键词（2-200 字符）')).toBeInTheDocument();
    expect(screen.getByText('信息搜索')).toBeInTheDocument();
  });

  it('shows disabled message when status is disabled', async () => {
    vi.mocked(api.webSearchApi.getStatus).mockResolvedValue(disabledStatus);
    render(<WebSearchPage onNavigate={vi.fn()} />);

    expect(await screen.findByText('搜索功能未启用')).toBeInTheDocument();
  });

  it('search button is disabled when keyword is empty', async () => {
    vi.mocked(api.webSearchApi.getStatus).mockResolvedValue(enabledStatus);
    render(<WebSearchPage onNavigate={vi.fn()} />);

    await screen.findByPlaceholderText('输入搜索关键词（2-200 字符）');
    const searchButton = screen.getByRole('button', { name: /搜.*索/ });
    expect(searchButton).toBeDisabled();
  });
});
