import {
  DashboardOutlined,
  DatabaseOutlined,
  EditOutlined,
  FileTextOutlined,
  FormOutlined,
  InfoCircleOutlined,
  SettingOutlined,
  SnippetsOutlined,
  UserOutlined,
  WechatOutlined,
} from '@ant-design/icons';
import { Layout, Menu, Modal, Typography } from 'antd';
import { useState } from 'react';
import { AuthProvider } from './auth/AuthContext';
import ProtectedRoute from './auth/ProtectedRoute';
import { useAuth } from './auth/useAuth';
import UserMenu from './components/UserMenu';
import AccountPage from './pages/AccountPage';
import ChatPage from './pages/ChatPage';
import Dashboard from './pages/Dashboard';
import DevLogsPage from './pages/DevLogsPage';
import LoginPage from './pages/LoginPage';
import LogsPage from './pages/LogsPage';
import PRTemplatesPage from './pages/PRTemplatesPage';
import ProductKnowledgePage from './pages/ProductKnowledgePage';
import ProfilePage from './pages/ProfilePage';
import SettingsPage from './pages/SettingsPage';

const { Header, Content } = Layout;
const { Text } = Typography;

const baseMenuItems = [
  { key: 'dashboard', icon: <DashboardOutlined />, label: '仪表盘' },
  { key: 'chat', icon: <EditOutlined />, label: '对话改稿' },
  { key: 'accounts', icon: <WechatOutlined />, label: '公众号账号' },
  { key: 'profile', icon: <UserOutlined />, label: '个人偏好' },
  {
    key: 'config',
    icon: <SettingOutlined />,
    label: '配置',
    children: [
      { key: 'pr-templates', icon: <SnippetsOutlined />, label: 'PR 模板' },
      { key: 'settings', icon: <FormOutlined />, label: '提示词配置' },
      { key: 'product-knowledge', icon: <DatabaseOutlined />, label: '产品知识库' },
    ],
  },
  { key: 'logs', icon: <FileTextOutlined />, label: '运行日志' },
  { key: 'about', icon: <InfoCircleOutlined />, label: '关于' },
];

export function MainLayout() {
  const [tab, setTab] = useState('dashboard');
  const [templateDirty, setTemplateDirty] = useState(false);
  const [settingsDirty, setSettingsDirty] = useState(false);
  const { user } = useAuth();
  const menuItems = user?.is_developer
    ? [
        ...baseMenuItems.slice(0, -1),
        { key: 'dev-logs', icon: <DatabaseOutlined />, label: '开发者日志' },
        baseMenuItems[baseMenuItems.length - 1],
      ]
    : baseMenuItems;

  const switchTab = (nextTab: string) => {
    const leavingDirtyTemplates = tab === 'pr-templates' && templateDirty;
    const leavingDirtySettings = tab === 'settings' && settingsDirty;
    if (nextTab !== tab && (leavingDirtyTemplates || leavingDirtySettings)) {
      Modal.confirm({
        title: leavingDirtyTemplates ? '离开模板编辑页面？' : '离开配置页面？',
        content: leavingDirtyTemplates
          ? '当前模板还有未保存内容，离开后修改将丢失。'
          : '当前提示词还有未保存内容，离开后修改将丢失。',
        okText: '放弃并离开',
        cancelText: '继续编辑',
        okButtonProps: { danger: true },
        onOk: () => {
          if (leavingDirtyTemplates) setTemplateDirty(false);
          if (leavingDirtySettings) setSettingsDirty(false);
          setTab(nextTab);
        },
      });
      return;
    }
    setTab(nextTab);
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
          onClick={({ key }) => switchTab(key)}
          items={menuItems}
          style={{ flex: 1, minWidth: 0 }}
        />
        <UserMenu />
      </Header>
      <Content>
        {tab === 'dashboard' && <Dashboard />}
        {tab === 'chat' && <ChatPage />}
        {tab === 'accounts' && <AccountPage />}
        {tab === 'logs' && <LogsPage />}
        {tab === 'profile' && <ProfilePage />}
        {tab === 'pr-templates' && <PRTemplatesPage onDirtyChange={setTemplateDirty} />}
        {tab === 'settings' && <SettingsPage onDirtyChange={setSettingsDirty} />}
        {tab === 'product-knowledge' && <ProductKnowledgePage />}
        {tab === 'dev-logs' && user?.is_developer && <DevLogsPage />}
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
