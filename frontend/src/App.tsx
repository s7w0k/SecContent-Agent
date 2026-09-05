import {
  DashboardOutlined,
  DatabaseOutlined,
  FormOutlined,
  SettingOutlined,
  SnippetsOutlined,
  RobotOutlined,
  UserOutlined,
  WechatOutlined,
} from '@ant-design/icons';
import { ConfigProvider, Layout, Menu, Modal } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import dayjs from 'dayjs';
import { useState } from 'react';
import 'dayjs/locale/zh-cn';
import { AuthProvider } from './auth/AuthContext';
import ProtectedRoute from './auth/ProtectedRoute';
import { useAuth } from './auth/useAuth';
import UserMenu from './components/UserMenu';
import AccountPage from './pages/AccountPage';
import AgentChatPage from './pages/AgentChatPage';
import Dashboard from './pages/Dashboard';
import DevLogsPage from './pages/DevLogsPage';
import LoginPage from './pages/LoginPage';
import PRTemplatesPage from './pages/PRTemplatesPage';
import ProductKnowledgePage from './pages/ProductKnowledgePage';
import ProfilePage from './pages/ProfilePage';
import SettingsPage from './pages/SettingsPage';

const { Header, Content } = Layout;

const baseMenuItems = [
  { key: 'agent-chat', icon: <RobotOutlined />, label: 'Agent 对话' },
  { key: 'dashboard', icon: <DashboardOutlined />, label: '仪表盘' },
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
];

export function MainLayout() {
  const [tab, setTab] = useState('agent-chat');
  const [templateDirty, setTemplateDirty] = useState(false);
  const [settingsDirty, setSettingsDirty] = useState(false);
  const [dashboardEntry] = useState<{ sourceType?: string; refreshKey: number }>({ refreshKey: 0 });
  const { user } = useAuth();
  const menuItems = user?.is_developer
    ? [...baseMenuItems, { key: 'dev-logs', icon: <DatabaseOutlined />, label: '开发者日志' }]
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
          background: '#ffffff',
          padding: '0 20px',
          height: 64,
          lineHeight: '64px',
          borderBottom: '1px solid #e8eaef',
          boxShadow: '0 1px 4px rgba(16,24,40,0.05)',
          position: 'sticky',
          top: 0,
          zIndex: 100,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginRight: 28 }}>
          <div
            style={{
              width: 36,
              height: 36,
              borderRadius: 10,
              background: 'linear-gradient(135deg,#6366f1,#8b5cf6)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: '#fff',
              fontSize: 18,
              boxShadow: '0 6px 14px rgba(99,102,241,0.35)',
              flex: 'none',
            }}
          >
            <RobotOutlined />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', lineHeight: 1.2 }}>
            <span style={{ fontWeight: 700, fontSize: 16, color: '#1f2329' }}>PR Agent</span>
            <span style={{ fontSize: 11, color: '#8a919e' }}>智能体安全 PR 工作台</span>
          </div>
        </div>
        <Menu
          theme="light"
          mode="horizontal"
          selectedKeys={[tab]}
          onClick={({ key }) => switchTab(key)}
          items={menuItems}
          style={{ flex: 1, minWidth: 0, background: 'transparent', fontWeight: 500, fontSize: 14 }}
        />
        <UserMenu />
      </Header>
      <Content>
        {tab === 'agent-chat' && <AgentChatPage />}
        {tab === 'dashboard' && (
          <Dashboard
            initialSourceType={dashboardEntry.sourceType}
            refreshKey={dashboardEntry.refreshKey}
          />
        )}
        {tab === 'accounts' && <AccountPage />}
        {tab === 'profile' && <ProfilePage />}
        {tab === 'pr-templates' && <PRTemplatesPage onDirtyChange={setTemplateDirty} />}
        {tab === 'settings' && <SettingsPage onDirtyChange={setSettingsDirty} />}
        {tab === 'product-knowledge' && <ProductKnowledgePage />}
        {tab === 'dev-logs' && user?.is_developer && <DevLogsPage />}
      </Content>
    </Layout>
  );
}

export default function App() {
  dayjs.locale('zh-cn');
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: '#6366f1',
          colorLink: '#6366f1',
          borderRadius: 8,
          fontSize: 14,
        },
      }}
    >
      <AuthProvider>
        <ProtectedRoute fallback={<LoginPage />}>
          <MainLayout />
        </ProtectedRoute>
      </AuthProvider>
    </ConfigProvider>
  );
}
