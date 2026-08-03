import { ControlOutlined, FileTextOutlined, FormOutlined } from '@ant-design/icons';
import { Card, Col, Menu, Row, Typography } from 'antd';
import { useState } from 'react';
import PromptEditor from '../components/PromptEditor';
import PromptEditorV2 from '../components/prompts/PromptEditorV2';
import GenerationPreferencesPage from './GenerationPreferencesPage';

const { Paragraph, Title } = Typography;

interface SettingsPageProps {
  onDirtyChange?: (dirty: boolean) => void;
}

const settingsItems = [
  {
    key: 'prompt-center',
    icon: <FormOutlined />,
    label: '提示词中心',
  },
  {
    key: 'draft-prompt',
    icon: <FileTextOutlined />,
    label: '初稿生成提示词（旧版）',
  },
  {
    key: 'generation-preferences',
    icon: <ControlOutlined />,
    label: '生成偏好',
  },
];

export default function SettingsPage({ onDirtyChange }: SettingsPageProps) {
  const [activeKey, setActiveKey] = useState('prompt-center');

  return (
    <div style={{ padding: 24, background: '#f5f7fb', minHeight: 'calc(100vh - 64px)' }}>
      <Title level={3} style={{ margin: 0 }}>
        配置
      </Title>
      <Paragraph type="secondary">维护当前账号的生成配置，后续配置项将在此处扩展。</Paragraph>
      <Row gutter={[16, 16]} align="top">
        <Col xs={24} md={6} lg={5}>
          <Card styles={{ body: { padding: 0 } }}>
            <Menu
              mode="inline"
              selectedKeys={[activeKey]}
              items={settingsItems}
              onClick={({ key }) => setActiveKey(key)}
            />
          </Card>
        </Col>
        <Col xs={24} md={18} lg={19}>
          {activeKey === 'prompt-center' && <PromptEditorV2 onDirtyChange={onDirtyChange} />}
          {activeKey === 'draft-prompt' && <PromptEditor onDirtyChange={onDirtyChange} />}
          {activeKey === 'generation-preferences' && <GenerationPreferencesPage />}
        </Col>
      </Row>
    </div>
  );
}
