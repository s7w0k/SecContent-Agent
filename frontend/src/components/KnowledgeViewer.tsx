/**
 * 知识库文档查看器
 *
 * 展示 Markdown 文档内容及其元数据（角色、是否核心打分文件、是否可编辑等）。
 */

import { LockOutlined, StarOutlined } from '@ant-design/icons';
import { Descriptions, Empty, Space, Tag, Typography } from 'antd';
import ReactMarkdown from 'react-markdown';
import type { KnowledgeDocument } from '../types';
import KnowledgeUsageBadge from './KnowledgeUsageBadge';

const { Paragraph, Text } = Typography;

interface KnowledgeViewerProps {
  document: KnowledgeDocument | null;
  loading?: boolean;
}

function formatSize(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(2)} MB`;
}

function formatTime(iso: string): string {
  if (!iso) return '-';
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function KnowledgeViewer({ document: doc, loading }: KnowledgeViewerProps) {
  if (loading) {
    return <Paragraph type="secondary">加载文档中...</Paragraph>;
  }

  if (!doc) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="选择左侧文件以查看内容"
        style={{ padding: '40px 0' }}
      />
    );
  }

  return (
    <div>
      <Space wrap style={{ marginBottom: 12 }}>
        <Text strong>{doc.name}</Text>
        <KnowledgeUsageBadge role={doc.knowledge_role} />
        {doc.direct_scoring_prompt && (
          <Tag icon={<StarOutlined />} color="gold">
            核心打分文件
          </Tag>
        )}
        {doc.loader_relevant && <Tag color="blue">评分相关</Tag>}
        {!doc.editable && (
          <Tag icon={<LockOutlined />} color="default">
            不可编辑
          </Tag>
        )}
        {doc.protected_path && <Tag color="red">受保护路径</Tag>}
      </Space>

      <Descriptions size="small" column={2} bordered style={{ marginBottom: 12 }}>
        <Descriptions.Item label="相对路径">{doc.relative_path}</Descriptions.Item>
        <Descriptions.Item label="文档ID">
          <Text code style={{ fontSize: 12 }}>
            {doc.document_id}
          </Text>
        </Descriptions.Item>
        <Descriptions.Item label="内容哈希">
          <Text code style={{ fontSize: 12 }}>
            {doc.content_hash?.slice(0, 12) || '-'}
          </Text>
        </Descriptions.Item>
        <Descriptions.Item label="大小">{formatSize(doc.size)}</Descriptions.Item>
        <Descriptions.Item label="更新时间">{formatTime(doc.updated_at)}</Descriptions.Item>
        <Descriptions.Item label="用途角色">{doc.knowledge_role}</Descriptions.Item>
      </Descriptions>

      <div
        style={{
          background: '#fafafa',
          border: '1px solid #f0f0f0',
          borderRadius: 4,
          padding: 16,
          maxHeight: '55vh',
          overflow: 'auto',
        }}
      >
        <ReactMarkdown>{doc.content || ''}</ReactMarkdown>
      </div>
    </div>
  );
}
