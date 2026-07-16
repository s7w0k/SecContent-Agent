import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { MainLayout } from './App';
import { AuthContext, type AuthContextValue } from './auth/AuthContext';

// Mock Dashboard page to avoid heavy sub-component loading
vi.mock('./pages/Dashboard', () => ({
  default: () => <div>Dashboard Page</div>,
}));
vi.mock('./pages/DevLogsPage', () => ({
  default: () => <div>Developer Logs Page</div>,
}));
vi.mock('./pages/PRTemplatesPage', () => ({
  default: ({ onDirtyChange }: { onDirtyChange?: (dirty: boolean) => void }) => (
    <button type="button" onClick={() => onDirtyChange?.(true)}>
      模拟未保存模板
    </button>
  ),
}));

describe('App', () => {
  const authValue: AuthContextValue = {
    user: {
      user_id: 'user-a',
      username: 'alice',
      display_name: 'Alice',
      is_developer: false,
      created_at: '2026-07-11T00:00:00Z',
    },
    token: 'token',
    loading: false,
    isAuthenticated: true,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    deleteAccount: vi.fn(),
  };

  const renderApp = () =>
    render(
      <AuthContext.Provider value={authValue}>
        <MainLayout />
      </AuthContext.Provider>,
    );

  it('renders app header', () => {
    renderApp();
    expect(screen.getByText('🛡 PR Agent')).toBeDefined();
  });

  it('shows dashboard tab by default', () => {
    renderApp();
    expect(screen.getByText('仪表盘')).toBeDefined();
    expect(screen.getByText('关于')).toBeDefined();
    expect(screen.getByText('PR 模板')).toBeDefined();
    expect(screen.queryByText('海外搜索')).not.toBeInTheDocument();
  });

  it('shows dashboard content by default', () => {
    renderApp();
    expect(screen.getByText('Dashboard Page')).toBeDefined();
  });

  it('hides developer logs from normal users', () => {
    renderApp();
    expect(screen.queryByText('开发者日志')).not.toBeInTheDocument();
  });

  it('shows developer logs to developer users', () => {
    render(
      <AuthContext.Provider
        value={{ ...authValue, user: authValue.user && { ...authValue.user, is_developer: true } }}
      >
        <MainLayout />
      </AuthContext.Provider>,
    );
    expect(screen.getByText('开发者日志')).toBeInTheDocument();
  });

  it('warns before navigating away from unsaved template changes', async () => {
    renderApp();
    fireEvent.click(screen.getByText('PR 模板'));
    fireEvent.click(await screen.findByText('模拟未保存模板'));
    fireEvent.click(screen.getByText('仪表盘'));

    expect((await screen.findAllByText('离开模板编辑页面？')).length).toBeGreaterThan(0);
    expect(screen.getByText('模拟未保存模板')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '继续编辑' }));
    expect(screen.getByText('模拟未保存模板')).toBeInTheDocument();
  });
});
