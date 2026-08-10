import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import SettingsPage from './SettingsPage';

vi.mock('../components/PromptEditor', () => ({
  default: ({ onDirtyChange }: { onDirtyChange?: (dirty: boolean) => void }) => (
    <button type="button" onClick={() => onDirtyChange?.(true)}>
      模拟提示词编辑器
    </button>
  ),
}));

vi.mock('../components/prompts/PromptEditorV2', () => ({
  default: ({ onDirtyChange }: { onDirtyChange?: (dirty: boolean) => void }) => (
    <button type="button" onClick={() => onDirtyChange?.(true)}>
      模拟提示词编辑器
    </button>
  ),
}));

describe('SettingsPage', () => {
  it('renders the extensible settings navigation and prompt editor', () => {
    render(<SettingsPage />);

    expect(screen.getByRole('heading', { name: '配置' })).toBeInTheDocument();
    expect(screen.getByText('初稿生成提示词（旧版）')).toBeInTheDocument();
    expect(screen.getByText('模拟提示词编辑器')).toBeInTheDocument();
  });

  it('forwards prompt dirty state to the parent', () => {
    const onDirtyChange = vi.fn();
    render(<SettingsPage onDirtyChange={onDirtyChange} />);

    fireEvent.click(screen.getByText('模拟提示词编辑器'));
    expect(onDirtyChange).toHaveBeenCalledWith(true);
  });
});
