import { FormOutlined } from '@ant-design/icons';
import { Card, Col, Menu, Row, Typography } from 'antd';
import PromptEditor from '../components/PromptEditor';

const { Paragraph, Title } = Typography;

interface SettingsPageProps {
  onDirtyChange?: (dirty: boolean) => void;
}

const settingsItems = [
  {
    key: 'draft-prompt',
    icon: <FormOutlined />,
    label: '初稿生成提示词',
  },
];

export default function SettingsPage({ onDirtyChange }: SettingsPageProps) {
  return (
    <div style={{ padding: 24, background: '#f5f7fb', minHeight: 'calc(100vh - 64px)' }}>
      <Title level={3} style={{ margin: 0 }}>
        配置
      </Title>
      <Paragraph type="secondary">维护当前账号的生成配置，后续配置项将在此处扩展。</Paragraph>
      <Row gutter={[16, 16]} align="top">
        <Col xs={24} md={6} lg={5}>
          <Card styles={{ body: { padding: 0 } }}>
            <Menu mode="inline" selectedKeys={['draft-prompt']} items={settingsItems} />
          </Card>
        </Col>
        <Col xs={24} md={18} lg={19}>
          <PromptEditor onDirtyChange={onDirtyChange} />
        </Col>
      </Row>
    </div>
  );
}
