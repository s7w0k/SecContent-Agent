import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { MainLayout } from './App';
import { AuthContext, type AuthContextValue } from './auth/AuthContext';

// Mock Dashboard page to avoid heavy sub-component loading
vi.mock('./pages/Dashboard', () => ({
  default: () => <div>Dashboard Page</div>,
}));

describe('App', () => {
  const authValue: AuthContextValue = {
    user: {
      user_id: 'user-a',
      username: 'alice',
      display_name: 'Alice',
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
  });

  it('shows dashboard content by default', () => {
    renderApp();
    expect(screen.getByText('Dashboard Page')).toBeDefined();
  });
});
