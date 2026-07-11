import {
  DashboardOutlined,
  DeleteOutlined,
  DownOutlined,
  EditOutlined,
  FileTextOutlined,
  InfoCircleOutlined,
  LogoutOutlined,
  SearchOutlined,
  UserOutlined,
  WechatOutlined,
} from '@ant-design/icons';
import { Avatar, Dropdown, Layout, Menu, Modal, Space, Typography, message } from 'antd';
import { useState } from 'react';
import { AuthProvider } from './auth/AuthContext';
import ProtectedRoute from './auth/ProtectedRoute';
import { useAuth } from './auth/useAuth';
import AccountPage from './pages/AccountPage';
import ChatPage from './pages/ChatPage';
import Dashboard from './pages/Dashboard';
import LoginPage from './pages/LoginPage';
import LogsPage from './pages/LogsPage';
import ProfilePage from './pages/ProfilePage';
import SearchPage from './pages/SearchPage';

const { Header, Content } = Layout;
const { Text } = Typography;

const menuItems = [
  { key: 'dashboard', icon: <DashboardOutlined />, label: '仪表盘' },
  { key: 'chat', icon: <EditOutlined />, label: '对话改稿' },
  { key: 'accounts', icon: <WechatOutlined />, label: '公众号账号' },
  { key: 'search', icon: <SearchOutlined />, label: '海外搜索' },
  { key: 'profile', icon: <UserOutlined />, label: '用户画像' },
  { key: 'logs', icon: <FileTextOutlined />, label: '运行日志' },
  { key: 'about', icon: <InfoCircleOutlined />, label: '关于' },
];

export function MainLayout() {
  const [tab, setTab] = useState('dashboard');
  const { user, logout, deleteAccount } = useAuth();

  const confirmDeleteAccount = () => {
    Modal.confirm({
      title: '确认注销账号？',
      content: '此操作将永久删除你的画像、草稿、反馈、对话和流水线记录，且无法恢复。',
      okText: '永久注销',
      cancelText: '取消',
      okType: 'danger',
      async onOk() {
        try {
          await deleteAccount();
          message.success('账号已注销');
        } catch {
          message.error('账号注销失败，请稍后重试');
          throw new Error('Account deletion failed');
        }
      },
    });
  };

  const userMenu = {
    items: [
      { key: 'logout', icon: <LogoutOutlined />, label: '退出登录' },
      { type: 'divider' as const },
      { key: 'delete', icon: <DeleteOutlined />, label: '注销账号', danger: true },
    ],
    onClick: ({ key }: { key: string }) => {
      if (key === 'logout') {
        logout();
        message.success('已退出登录');
      } else if (key === 'delete') {
        confirmDeleteAccount();
      }
    },
  };

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header
        style={{
          display: 'flex',
          alignItems: 'center',
          background: '#001529',
          padding: '0 24px',
        }}
      >
        <Text strong style={{ color: '#fff', fontSize: 18, marginRight: 32 }}>
          🛡 PR Agent
        </Text>
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={[tab]}
          onClick={({ key }) => setTab(key)}
          items={menuItems}
          style={{ flex: 1, minWidth: 0 }}
        />
        <Dropdown menu={userMenu} placement="bottomRight">
          <Space style={{ color: '#fff', cursor: 'pointer', marginLeft: 16 }}>
            <Avatar size="small" icon={<UserOutlined />} />
            <span>{user?.display_name || user?.username}</span>
            <DownOutlined style={{ fontSize: 10 }} />
          </Space>
        </Dropdown>
      </Header>
      <Content>
        {tab === 'dashboard' && <Dashboard />}
        {tab === 'chat' && <ChatPage />}
        {tab === 'accounts' && <AccountPage />}
        {tab === 'logs' && <LogsPage />}
        {tab === 'search' && <SearchPage />}
        {tab === 'profile' && <ProfilePage />}
        {tab === 'about' && (
          <div style={{ padding: 48, maxWidth: 800, margin: '0 auto' }}>
            <h2>🚀 PR Agent Dashboard</h2>
            <p>智能体安全PR情报Agent系统</p>
            <ul>
              <li>MongoDB</li>
              <li>mcp-wewe</li>
              <li>mcp-crawl</li>
              <li>Backend (FastAPI + Agent Pipeline)</li>
              <li>Frontend (React + Ant Design)</li>
            </ul>
          </div>
        )}
      </Content>
    </Layout>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <ProtectedRoute fallback={<LoginPage />}>
        <MainLayout />
      </ProtectedRoute>
    </AuthProvider>
  );
}
