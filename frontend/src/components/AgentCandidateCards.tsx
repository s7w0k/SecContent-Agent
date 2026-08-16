import { CheckCircleOutlined, LinkOutlined } from '@ant-design/icons';
import { Empty, List, Radio, Space, Tag, Typography } from 'antd';
import type { AgentCandidate } from '../types';

const { Text, Paragraph } = Typography;

interface AgentCandidateCardsProps {
  candidates: AgentCandidate[];
  selectedId?: string;
  onSelect: (candidate: AgentCandidate) => void;
}

/** Structured candidate cards for ambiguous news selection in a conversation turn. */
export default function AgentCandidateCards({
  candidates,
  selectedId,
  onSelect,
}: AgentCandidateCardsProps) {
  if (candidates.length === 0) {
    return <Empty description="没有找到匹配的新闻" image={Empty.PRESENTED_IMAGE_SIMPLE} />;
  }

  return (
    <Radio.Group
      aria-label="候选新闻"
      value={selectedId}
      onChange={(event) => {
        const candidate = candidates.find((item) => item.article_id === event.target.value);
        if (candidate) onSelect(candidate);
      }}
      style={{ width: '100%' }}
    >
      <List
        dataSource={candidates}
        split
        renderItem={(candidate, index) => (
          <List.Item key={candidate.article_id} style={{ alignItems: 'flex-start' }}>
            <Space align="start" size={12} style={{ width: '100%' }}>
              <Radio value={candidate.article_id} aria-label={`选择第 ${index + 1} 条`} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <Space size={6} wrap>
                  <Text strong>{candidate.title || '未命名新闻'}</Text>
                  {candidate.score !== null && candidate.score !== undefined && (
                    <Tag color="blue">匹配度 {Math.round(candidate.score * 100)}%</Tag>
                  )}
                  {selectedId === candidate.article_id && (
                    <Tag icon={<CheckCircleOutlined />} color="success">
                      已选择
                    </Tag>
                  )}
                </Space>
                <div>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {candidate.source || '未知来源'}
                    {candidate.published_at ? ` · ${candidate.published_at.slice(0, 10)}` : ''}
                  </Text>
                </div>
                <Paragraph
                  ellipsis={{ rows: 2, expandable: true, symbol: '展开' }}
                  style={{ margin: '4px 0 0', fontSize: 13 }}
                >
                  {candidate.summary || '暂无摘要'}
                </Paragraph>
                {candidate.source_ref && (
                  <a href={candidate.source_ref} target="_blank" rel="noopener noreferrer">
                    <LinkOutlined /> 原文
                  </a>
                )}
              </div>
            </Space>
          </List.Item>
        )}
      />
    </Radio.Group>
  );
}
