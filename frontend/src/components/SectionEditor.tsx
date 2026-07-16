import {
  DeleteOutlined,
  DownOutlined,
  HolderOutlined,
  PlusOutlined,
  UpOutlined,
} from '@ant-design/icons';
import { Button, Card, Col, Form, Input, Row, Space, Typography, message } from 'antd';
import { useRef } from 'react';

const { Text } = Typography;

export default function SectionEditor() {
  const dragIndex = useRef<number | null>(null);

  return (
    <Form.List name="sections">
      {(fields, { add, remove, move }) => (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {fields.map((field, index) => (
            <Card
              key={field.key}
              size="small"
              draggable
              onDragStart={() => {
                dragIndex.current = index;
              }}
              onDragOver={(event) => event.preventDefault()}
              onDrop={() => {
                if (dragIndex.current !== null && dragIndex.current !== index) {
                  move(dragIndex.current, index);
                }
                dragIndex.current = null;
              }}
              title={
                <Space>
                  <HolderOutlined style={{ cursor: 'grab' }} />
                  <Text>章节 {index + 1}</Text>
                </Space>
              }
              extra={
                <Space size={4}>
                  <Button
                    aria-label={`上移章节 ${index + 1}`}
                    type="text"
                    icon={<UpOutlined />}
                    disabled={index === 0}
                    onClick={() => move(index, index - 1)}
                  />
                  <Button
                    aria-label={`下移章节 ${index + 1}`}
                    type="text"
                    icon={<DownOutlined />}
                    disabled={index === fields.length - 1}
                    onClick={() => move(index, index + 1)}
                  />
                  <Button
                    aria-label={`删除章节 ${index + 1}`}
                    type="text"
                    danger
                    icon={<DeleteOutlined />}
                    disabled={fields.length <= 1}
                    onClick={() => remove(field.name)}
                  />
                </Space>
              }
            >
              <Row gutter={12}>
                <Col span={8}>
                  <Form.Item
                    label="章节标题"
                    name={[field.name, 'heading']}
                    rules={[
                      { required: true, whitespace: true, message: '请输入章节标题' },
                      { max: 100, message: '章节标题不能超过 100 个字符' },
                    ]}
                  >
                    <Input placeholder="例如：事件概述" />
                  </Form.Item>
                </Col>
                <Col span={16}>
                  <Form.Item
                    label="写作指引"
                    name={[field.name, 'guide']}
                    rules={[
                      { required: true, whitespace: true, message: '请输入写作指引' },
                      { max: 1000, message: '写作指引不能超过 1000 个字符' },
                    ]}
                  >
                    <Input.TextArea rows={2} placeholder="说明本章节应包含的内容" />
                  </Form.Item>
                </Col>
              </Row>
            </Card>
          ))}
          <Button
            block
            type="dashed"
            icon={<PlusOutlined />}
            onClick={() => {
              if (fields.length >= 12) {
                message.warning('最多允许 12 个章节');
                return;
              }
              add({ heading: '', guide: '', order: fields.length + 1 });
            }}
          >
            添加章节
          </Button>
        </Space>
      )}
    </Form.List>
  );
}
