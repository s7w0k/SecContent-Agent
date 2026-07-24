import { Alert, Button, List, Space, Tag, Typography, message } from 'antd';
import { useState } from 'react';

const { Text } = Typography;

interface PersonalizationExplanationProps {
  hardPreferences: string[];
  softPreferences: Array<{ memory_id: string; text: string; confidence: number }>;
  avoidPatterns: string[];
  generationId?: string;
}

const FEEDBACK_OPTIONS = [
  { value: 'helpful', label: '有帮助' },
  { value: 'no_effect', label: '无影响' },
  { value: 'incorrect', label: '不符合' },
  { value: 'never_use', label: '不要再使用' },
];

export default function PersonalizationExplanation({
  hardPreferences,
  softPreferences,
  avoidPatterns,
  generationId,
}: PersonalizationExplanationProps) {
  const [feedbackMap, setFeedbackMap] = useState<Record<string, string>>({});

  const totalCount = hardPreferences.length + softPreferences.length + avoidPatterns.length;

  const handleFeedback = (key: string, feedback: string) => {
    setFeedbackMap((prev) => ({ ...prev, [key]: feedback }));
    message.success('感谢反馈');
  };

  const renderFeedbackButtons = (key: string) => (
    <Space size={4} wrap>
      {FEEDBACK_OPTIONS.map((opt) => (
        <Button
          key={opt.value}
          size="small"
          type={feedbackMap[key] === opt.value ? 'primary' : 'default'}
          onClick={() => handleFeedback(key, opt.value)}
        >
          {opt.label}
        </Button>
      ))}
    </Space>
  );

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Alert
        showIcon
        type="info"
        message={`本稿使用了 ${totalCount} 条个性化偏好`}
        description={generationId ? `生成批次：${generationId}` : undefined}
      />

      <List
        size="small"
        header={<Text strong>硬性偏好</Text>}
        locale={{ emptyText: '无硬性偏好' }}
        dataSource={hardPreferences}
        renderItem={(text, index) => (
          <List.Item>
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              <Space>
                <Tag color="purple">硬性</Tag>
                <Text>{text}</Text>
              </Space>
              {renderFeedbackButtons(`hard-${index}`)}
            </Space>
          </List.Item>
        )}
      />

      <List
        size="small"
        header={<Text strong>软性偏好</Text>}
        locale={{ emptyText: '无软性偏好' }}
        dataSource={softPreferences}
        renderItem={(item) => (
          <List.Item>
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              <Space>
                <Tag color="blue">软性</Tag>
                <Text>{item.text}</Text>
                <Text type="secondary">置信度 {Math.round(item.confidence * 100)}%</Text>
              </Space>
              {renderFeedbackButtons(`soft-${item.memory_id}`)}
            </Space>
          </List.Item>
        )}
      />

      <List
        size="small"
        header={<Text strong>规避模式</Text>}
        locale={{ emptyText: '无规避模式' }}
        dataSource={avoidPatterns}
        renderItem={(text, index) => (
          <List.Item>
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              <Space>
                <Tag color="red">规避</Tag>
                <Text>{text}</Text>
              </Space>
              {renderFeedbackButtons(`avoid-${index}`)}
            </Space>
          </List.Item>
        )}
      />
    </Space>
  );
}
