import { Spin } from 'antd';
import type { ReactNode } from 'react';
import { useAuth } from './useAuth';

interface ProtectedRouteProps {
  children: ReactNode;
  fallback: ReactNode;
}

export default function ProtectedRoute({ children, fallback }: ProtectedRouteProps) {
  const { loading, isAuthenticated } = useAuth();
  if (loading) {
    return (
      <output
        aria-label="正在恢复登录状态"
        style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}
      >
        <Spin size="large" tip="正在验证登录状态..." />
      </output>
    );
  }
  return isAuthenticated ? children : fallback;
}
