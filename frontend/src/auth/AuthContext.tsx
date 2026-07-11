import { type ReactNode, createContext, useCallback, useEffect, useMemo, useState } from 'react';
import { ACCESS_TOKEN_KEY, AUTH_UNAUTHORIZED_EVENT, authApi, getAccessToken } from '../api/client';
import type { LoginRequest, RegisterRequest, User } from '../types';

export interface AuthContextValue {
  user: User | null;
  token: string | null;
  loading: boolean;
  isAuthenticated: boolean;
  login: (payload: LoginRequest) => Promise<void>;
  register: (payload: RegisterRequest) => Promise<void>;
  logout: () => void;
  deleteAccount: (password?: string) => Promise<void>;
}

export const AuthContext = createContext<AuthContextValue | null>(null);

interface AuthProviderProps {
  children: ReactNode;
}

export function AuthProvider({ children }: AuthProviderProps) {
  const [user, setUser] = useState<User | null>(null);
  const [token, setToken] = useState<string | null>(() => getAccessToken());
  const [loading, setLoading] = useState(true);

  const logout = useCallback(() => {
    window.localStorage.removeItem(ACCESS_TOKEN_KEY);
    setToken(null);
    setUser(null);
  }, []);

  useEffect(() => {
    const handleUnauthorized = () => logout();
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, handleUnauthorized);
    return () => window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, handleUnauthorized);
  }, [logout]);

  useEffect(() => {
    const restoreSession = async () => {
      const storedToken = getAccessToken();
      if (!storedToken) {
        setLoading(false);
        return;
      }
      try {
        const currentUser = await authApi.me();
        setToken(storedToken);
        setUser(currentUser);
      } catch {
        logout();
      } finally {
        setLoading(false);
      }
    };
    void restoreSession();
  }, [logout]);

  const login = useCallback(async (payload: LoginRequest) => {
    const response = await authApi.login(payload);
    window.localStorage.setItem(ACCESS_TOKEN_KEY, response.access_token);
    setToken(response.access_token);
    setUser(response.user);
  }, []);

  const register = useCallback(
    async (payload: RegisterRequest) => {
      await authApi.register(payload);
      await login({ username: payload.username, password: payload.password });
    },
    [login],
  );

  const deleteAccount = useCallback(
    async (password?: string) => {
      await authApi.deleteAccount(password);
      logout();
    },
    [logout],
  );

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      token,
      loading,
      isAuthenticated: Boolean(token && user),
      login,
      register,
      logout,
      deleteAccount,
    }),
    [user, token, loading, login, register, logout, deleteAccount],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
