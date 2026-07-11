import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ACCESS_TOKEN_KEY } from '../api/client';
import { AuthProvider } from './AuthContext';
import { useAuth } from './useAuth';

const mocks = vi.hoisted(() => ({
  login: vi.fn(),
  register: vi.fn(),
  me: vi.fn(),
  deleteAccount: vi.fn(),
}));

vi.mock('../api/client', () => ({
  ACCESS_TOKEN_KEY: 'access_token',
  AUTH_UNAUTHORIZED_EVENT: 'auth:unauthorized',
  getAccessToken: () => window.localStorage.getItem('access_token'),
  authApi: mocks,
}));

const user = {
  user_id: 'user-1',
  username: 'alice',
  display_name: 'Alice',
  email: 'alice@example.com',
  created_at: '2026-07-11T00:00:00Z',
};

function AuthHarness() {
  const auth = useAuth();
  return (
    <div>
      <span data-testid="loading">{String(auth.loading)}</span>
      <span data-testid="authenticated">{String(auth.isAuthenticated)}</span>
      <span data-testid="username">{auth.user?.username || ''}</span>
      <button
        type="button"
        onClick={() => void auth.login({ username: 'alice', password: 'secret1' })}
      >
        login
      </button>
      <button type="button" onClick={auth.logout}>
        logout
      </button>
      <button type="button" onClick={() => void auth.deleteAccount('secret1')}>
        delete
      </button>
    </div>
  );
}

function renderProvider() {
  return render(
    <AuthProvider>
      <AuthHarness />
    </AuthProvider>,
  );
}

describe('AuthContext', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
  });

  it('starts unauthenticated when no token is stored', async () => {
    renderProvider();

    await waitFor(() => expect(screen.getByTestId('loading')).toHaveTextContent('false'));
    expect(screen.getByTestId('authenticated')).toHaveTextContent('false');
    expect(mocks.me).not.toHaveBeenCalled();
  });

  it('logs in and persists the access token', async () => {
    mocks.login.mockResolvedValue({ access_token: 'jwt-login', token_type: 'bearer', user });
    renderProvider();
    await waitFor(() => expect(screen.getByTestId('loading')).toHaveTextContent('false'));

    fireEvent.click(screen.getByRole('button', { name: 'login' }));

    await waitFor(() => expect(screen.getByTestId('authenticated')).toHaveTextContent('true'));
    expect(screen.getByTestId('username')).toHaveTextContent('alice');
    expect(window.localStorage.getItem(ACCESS_TOKEN_KEY)).toBe('jwt-login');
  });

  it('logs out and removes the persisted session', async () => {
    window.localStorage.setItem(ACCESS_TOKEN_KEY, 'jwt-existing');
    mocks.me.mockResolvedValue(user);
    renderProvider();
    await waitFor(() => expect(screen.getByTestId('authenticated')).toHaveTextContent('true'));

    fireEvent.click(screen.getByRole('button', { name: 'logout' }));

    expect(screen.getByTestId('authenticated')).toHaveTextContent('false');
    expect(window.localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
  });

  it('deletes the account and clears authentication', async () => {
    window.localStorage.setItem(ACCESS_TOKEN_KEY, 'jwt-existing');
    mocks.me.mockResolvedValue(user);
    mocks.deleteAccount.mockResolvedValue({ ok: true });
    renderProvider();
    await waitFor(() => expect(screen.getByTestId('authenticated')).toHaveTextContent('true'));

    fireEvent.click(screen.getByRole('button', { name: 'delete' }));

    await waitFor(() => expect(mocks.deleteAccount).toHaveBeenCalledWith('secret1'));
    expect(screen.getByTestId('authenticated')).toHaveTextContent('false');
    expect(window.localStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
  });

  it('restores the current user from a stored token', async () => {
    window.localStorage.setItem(ACCESS_TOKEN_KEY, 'jwt-restored');
    mocks.me.mockResolvedValue(user);

    renderProvider();

    await waitFor(() => expect(screen.getByTestId('authenticated')).toHaveTextContent('true'));
    expect(mocks.me).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('username')).toHaveTextContent('alice');
  });
});
