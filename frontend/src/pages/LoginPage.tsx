import { LockOutlined, SafetyCertificateOutlined, UserOutlined } from '@ant-design/icons';
import { Alert, Button, Card, Form, Input, Segmented, Space, Typography, message } from 'antd';
import { useState } from 'react';
import { useAuth } from '../auth/useAuth';

const { Paragraph, Text, Title } = Typography;

type AuthMode = 'login' | 'register';

interface AuthFormValues {
  username: string;
  password: string;
  confirm_password?: string;
  display_name?: string;
  email?: string;
}

function errorMessage(error: unknown): string {
  if (typeof error !== 'object' || error === null) return '请求失败，请稍后重试';
  const response = 'response' in error ? error.response : undefined;
  if (typeof response === 'object' && response !== null && 'data' in response) {
    const data = response.data;
    if (typeof data === 'object' && data !== null) {
      if ('error' in data && typeof data.error === 'object' && data.error !== null) {
        const nestedMessage = 'message' in data.error ? data.error.message : undefined;
        if (typeof nestedMessage === 'string') return nestedMessage;
      }
      if ('detail' in data && typeof data.detail === 'string') return data.detail;
    }
  }
  return error instanceof Error ? error.message : '请求失败，请稍后重试';
}

export default function LoginPage() {
  const [mode, setMode] = useState<AuthMode>('login');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [form] = Form.useForm<AuthFormValues>();
  const { login, register } = useAuth();

  const switchMode = (value: string | number) => {
    setMode(value as AuthMode);
    setError('');
    form.resetFields();
  };

  const submit = async (values: AuthFormValues) => {
    setSubmitting(true);
    setError('');
    try {
      if (mode === 'login') {
        await login({ username: values.username, password: values.password });
        message.success('登录成功');
      } else {
        await register({
          username: values.username,
          password: values.password,
          display_name: values.display_name || values.username,
          email: values.email || undefined,
        });
        message.success('注册并登录成功');
      }
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'grid',
        placeItems: 'center',
        padding: 24,
        background: 'linear-gradient(135deg, #001529 0%, #0b3a67 55%, #1677ff 100%)',
      }}
    >
      <Card style={{ width: '100%', maxWidth: 430, borderRadius: 16 }}>
        <Space direction="vertical" size={4} style={{ width: '100%', textAlign: 'center' }}>
          <SafetyCertificateOutlined style={{ color: '#1677ff', fontSize: 44 }} />
          <Title level={2} style={{ margin: '4px 0 0' }}>
            PR Agent
          </Title>
          <Paragraph type="secondary">智能体身份安全 PR 情报工作台</Paragraph>
        </Space>

        <Segmented
          block
          value={mode}
          onChange={switchMode}
          options={[
            { label: '登录', value: 'login' },
            { label: '注册', value: 'register' },
          ]}
          style={{ marginBottom: 24 }}
        />

        {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}

        <Form form={form} layout="vertical" onFinish={submit} requiredMark={false}>
          <Form.Item
            name="username"
            label="用户名"
            rules={[
              { required: true, message: '请输入用户名' },
              { pattern: /^[A-Za-z0-9_]{3,20}$/, message: '请输入 3-20 位字母、数字或下划线' },
            ]}
          >
            <Input prefix={<UserOutlined />} autoComplete="username" placeholder="用户名" />
          </Form.Item>

          {mode === 'register' && (
            <>
              <Form.Item name="display_name" label="显示名称">
                <Input maxLength={50} placeholder="默认使用用户名" />
              </Form.Item>
              <Form.Item
                name="email"
                label="邮箱"
                rules={[{ type: 'email', message: '邮箱格式不正确' }]}
              >
                <Input autoComplete="email" placeholder="可选" />
              </Form.Item>
            </>
          )}

          <Form.Item
            name="password"
            label="密码"
            rules={[
              { required: true, message: '请输入密码' },
              { min: 6, max: 32, message: '密码长度须为 6-32 位' },
            ]}
          >
            <Input.Password
              prefix={<LockOutlined />}
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              placeholder="密码"
            />
          </Form.Item>

          {mode === 'register' && (
            <Form.Item
              name="confirm_password"
              label="确认密码"
              dependencies={['password']}
              rules={[
                { required: true, message: '请再次输入密码' },
                ({ getFieldValue }) => ({
                  validator(_, value) {
                    return !value || getFieldValue('password') === value
                      ? Promise.resolve()
                      : Promise.reject(new Error('两次输入的密码不一致'));
                  },
                }),
              ]}
            >
              <Input.Password
                prefix={<LockOutlined />}
                autoComplete="new-password"
                placeholder="再次输入密码"
              />
            </Form.Item>
          )}

          <Button type="primary" htmlType="submit" loading={submitting} block size="large">
            {mode === 'login' ? '登录' : '注册并登录'}
          </Button>
        </Form>

        <Text type="secondary" style={{ display: 'block', textAlign: 'center', marginTop: 20 }}>
          登录即表示你同意仅将本系统用于授权的安全情报工作
        </Text>
      </Card>
    </div>
  );
}
