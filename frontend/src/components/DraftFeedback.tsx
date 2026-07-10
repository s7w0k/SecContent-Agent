import { SendOutlined } from '@ant-design/icons';
import { Button, Card, Input, Rate, Space, Tag, Typography, message } from 'antd';
import { useMemo, useState } from 'react';
import api from '../api/client';
import type { FeedbackCreate, FeedbackCreateResponse, TargetType } from '../types';

const { Text } = Typography;
const { TextArea } = Input;
const { CheckableTag } = Tag;

const PRESET_TAGS = [
  '标题不够有冲击力',
  '导语需要更清晰',
  '技术细节太多',
  '业务价值不突出',
  '语气太硬',
  '篇幅偏长',
  '结构清晰',
  '可直接使用',
];

export interface DraftFeedbackProps {
  articleUrlHash: string;
  draftIndex: number;
  template: string;
  perspective: string;
  revisionId?: string;
  initialRating?: number | null;
  compact?: boolean;
  onSubmitted?: (feedbackId: string) => void;
}

export default function DraftFeedback({
  articleUrlHash,
  draftIndex,
  template,
  perspective,
  revisionId,
  initialRating,
  compact = false,
  onSubmitted,
}: DraftFeedbackProps) {
  const [rating, setRating] = useState(initialRating ?? 0);
  const [comment, setComment] = useState('');
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  const targetType: TargetType = revisionId ? 'revision' : 'draft';
  const canSubmit = rating > 0 && !submitting;

  const title = useMemo(() => {
    return revisionId ? '修订稿反馈' : '草稿反馈';
  }, [revisionId]);

  const toggleTag = (tag: string, checked: boolean) => {
    setSelectedTags((prev) => (checked ? [...prev, tag] : prev.filter((item) => item !== tag)));
  };

  const handleSubmit = async () => {
    if (!canSubmit) {
      message.warning('请先选择 1-5 星评分');
      return;
    }

    const payload: FeedbackCreate = {
      target_type: targetType,
      target_ref: {
        article_url_hash: articleUrlHash,
        draft_index: draftIndex,
        revision_id: revisionId,
      },
      rating,
      comment: comment.trim(),
      tags: selectedTags,
    };

    setSubmitting(true);
    try {
      const result: FeedbackCreateResponse = await api.create(payload);
      setSubmitted(true);
      message.success('感谢反馈，系统会用于后续风格学习');
      onSubmitted?.(result.feedback_id);
    } catch {
      message.error('反馈提交失败，请稍后重试');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Card
      size="small"
      title={title}
      style={{ marginTop: compact ? 12 : 16 }}
      styles={{ body: { padding: compact ? 12 : 16 } }}
    >
      <Space direction="vertical" size={compact ? 8 : 12} style={{ width: '100%' }}>
        <Space wrap>
          <Rate value={rating} onChange={setRating} />
          {initialRating ? (
            <Text type="secondary">历史评分：{initialRating} 星</Text>
          ) : (
            <Text type="secondary">请选择满意度评分</Text>
          )}
        </Space>

        <Space size={[0, 8]} wrap>
          {PRESET_TAGS.map((tag) => (
            <CheckableTag
              key={tag}
              checked={selectedTags.includes(tag)}
              onChange={(checked) => toggleTag(tag, checked)}
            >
              {tag}
            </CheckableTag>
          ))}
        </Space>

        <TextArea
          value={comment}
          onChange={(event) => setComment(event.target.value)}
          placeholder="补充你的具体意见，例如：标题还可以更抓人，或希望减少技术细节。"
          autoSize={{ minRows: compact ? 2 : 3, maxRows: 5 }}
          maxLength={2000}
          showCount
        />

        <Space style={{ justifyContent: 'space-between', width: '100%' }}>
          <Text type="secondary">
            {template} / {perspective}
          </Text>
          <Button
            type="primary"
            icon={<SendOutlined />}
            loading={submitting}
            disabled={!canSubmit || submitted}
            onClick={handleSubmit}
          >
            {submitted ? '已提交' : '提交反馈'}
          </Button>
        </Space>
      </Space>
    </Card>
  );
}
