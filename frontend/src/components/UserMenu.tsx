import { DeleteOutlined, DownOutlined, LogoutOutlined, UserOutlined } from '@ant-design/icons';
import { Avatar, Dropdown, Modal, Space, message } from 'antd';
import { useAuth } from '../auth/useAuth';

export default function UserMenu() {
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

  return (
    <Dropdown
      placement="bottomRight"
      menu={{
        items: [
          { key: 'logout', icon: <LogoutOutlined />, label: '退出登录' },
          { type: 'divider' },
          { key: 'delete', icon: <DeleteOutlined />, label: '注销账号', danger: true },
        ],
        onClick: ({ key }) => {
          if (key === 'logout') {
            logout();
            message.success('已退出登录');
          } else if (key === 'delete') {
            confirmDeleteAccount();
          }
        },
      }}
    >
      <Space style={{ color: '#fff', cursor: 'pointer', marginLeft: 16 }}>
        <Avatar size="small" icon={<UserOutlined />} />
        <span>{user?.display_name || user?.username}</span>
        <DownOutlined style={{ fontSize: 10 }} />
      </Space>
    </Dropdown>
  );
}
