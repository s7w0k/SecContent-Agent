import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import FilterBar from './FilterBar';

describe('FilterBar', () => {
  const defaultProps = {
    value: {},
    onChange: vi.fn(),
    categories: ['MCP协议漏洞', '提示注入', 'AI安全', 'Agent安全'],
  };

  it('renders source and category selects', () => {
    render(<FilterBar {...defaultProps} />);
    expect(screen.getByText('全部来源')).toBeDefined();
    expect(screen.getByText('全部分类')).toBeDefined();
  });

  it('renders score input', () => {
    render(<FilterBar {...defaultProps} />);
    expect(screen.getByPlaceholderText('最低分')).toBeDefined();
  });

  it('renders keyword search input', () => {
    render(<FilterBar {...defaultProps} />);
    expect(screen.getByPlaceholderText('关键词搜索')).toBeDefined();
  });

  it('renders reset button', () => {
    render(<FilterBar {...defaultProps} />);
    expect(screen.getByText('重置')).toBeDefined();
  });

  it('offers user upload as a source filter', async () => {
    render(<FilterBar {...defaultProps} />);
    fireEvent.mouseDown(screen.getByText('全部来源'));
    expect(await screen.findByText('用户上传')).toBeDefined();
  });
});
