import { CopyOutlined, ReloadOutlined, SafetyCertificateOutlined } from '@ant-design/icons';
import { Alert, Button, Card, Empty, Segmented, Space, Spin, Tag, Typography, message } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import type {
  DraftReview,
  DraftReviewIssue,
  DraftReviewIssueCategory,
  DraftReviewSeverity,
} from '../types';
import styles from './DraftReviewPanel.module.css';

const { Paragraph, Text } = Typography;

type ReviewFilter = 'all' | 'fact' | 'wording';

const FACT_CATEGORIES = new Set<DraftReviewIssueCategory>([
  'fact_mismatch',
  'unsupported_claim',
  'internal_conflict',
  'unsupported_data',
]);

const CATEGORY_LABELS: Record<DraftReviewIssueCategory, string> = {
  fact_mismatch: '与原文不一致',
  unsupported_claim: '原文无依据结论',
  internal_conflict: '稿件前后矛盾',
  absolute_claim: '绝对化表达',
  competitor_comparison: '竞品比较',
  competitor_disparagement: '竞品贬损',
  guarantee_claim: '保证性话术',
  unsupported_data: '无来源数据',
  exaggerated_claim: '夸大表达',
  ambiguous_expression: '表述不严谨',
};

const SEVERITY_META: Record<DraftReviewSeverity, { label: string; color: string }> = {
  high: { label: '必须修改', color: 'red' },
  medium: { label: '建议修改', color: 'orange' },
  low: { label: '表达优化', color: 'blue' },
};

interface DraftReviewPanelProps {
  review?: DraftReview;
  contentMd: string;
  reviewing?: boolean;
  onReview?: () => void | Promise<void>;
  compact?: boolean;
}

export async function computeDraftContentHash(content: string): Promise<string> {
  const normalized = content.replace(/\r\n/g, '\n').replace(/\r/g, '\n').trim();
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) throw new Error('Web Crypto is unavailable');
  const digest = await subtle.digest('SHA-256', new TextEncoder().encode(normalized));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

function statusTag(review: DraftReview | undefined, reviewing: boolean, stale: boolean) {
  if (reviewing) return <Tag color="processing">检查中</Tag>;
  if (stale) return <Tag color="warning">已过期</Tag>;
  if (!review) return <Tag>未检查</Tag>;
  if (review.status === 'failed') return <Tag color="error">检查失败</Tag>;
  if (review.status === 'partial') return <Tag color="warning">部分完成</Tag>;
  return <Tag color="success">已完成</Tag>;
}

function ReviewIssueItem({ issue }: { issue: DraftReviewIssue }) {
  const severity = SEVERITY_META[issue.severity];

  const copyRewrite = async () => {
    if (!issue.suggested_rewrite) return;
    try {
      await navigator.clipboard.writeText(issue.suggested_rewrite);
      message.success('推荐改写已复制');
    } catch {
      message.error('复制失败');
    }
  };

  return (
    <div className={styles.issueItem} data-severity={issue.severity}>
      <Space wrap size={[4, 4]}>
        <Tag color={severity.color}>{severity.label}</Tag>
        <Tag>{CATEGORY_LABELS[issue.category]}</Tag>
      </Space>
      <blockquote className={styles.quote}>{issue.quote}</blockquote>
      <Paragraph className={styles.detail}>
        <Text strong>问题：</Text>
        {issue.reason}
      </Paragraph>
      <Paragraph className={styles.detail}>
        <Text strong>建议：</Text>
        {issue.suggestion}
      </Paragraph>
      {issue.suggested_rewrite && (
        <div className={styles.rewrite}>
          <Text>{issue.suggested_rewrite}</Text>
          <Button
            type="text"
            size="small"
            aria-label={`复制推荐改写：${issue.quote}`}
            icon={<CopyOutlined />}
            onClick={copyRewrite}
          >
            复制
          </Button>
        </div>
      )}
    </div>
  );
}

export default function DraftReviewPanel({
  review,
  contentMd,
  reviewing = false,
  onReview,
  compact = false,
}: DraftReviewPanelProps) {
  const [filter, setFilter] = useState<ReviewFilter>('all');
  const [stale, setStale] = useState(false);

  useEffect(() => {
    let active = true;
    setStale(false);
    if (!review?.content_hash) return () => undefined;
    void computeDraftContentHash(contentMd)
      .then((hash) => {
        if (active) setStale(hash !== review.content_hash);
      })
      .catch(() => {
        if (active) setStale(false);
      });
    return () => {
      active = false;
    };
  }, [contentMd, review?.content_hash]);

  const issues = useMemo(() => {
    const allIssues = review?.issues ?? [];
    if (filter === 'all') return allIssues;
    return allIssues.filter((issue) =>
      filter === 'fact'
        ? FACT_CATEGORIES.has(issue.category)
        : !FACT_CATEGORIES.has(issue.category),
    );
  }, [filter, review?.issues]);

  const reviewButton = onReview ? (
    <Button
      size="small"
      icon={<ReloadOutlined />}
      onClick={() => void onReview()}
      loading={reviewing}
    >
      {review ? '重新检查' : '开始检查'}
    </Button>
  ) : null;

  return (
    <Card
      size="small"
      className={`${styles.panel} ${compact ? styles.compact : ''}`}
      title={
        <Space>
          <SafetyCertificateOutlined />
          <span>内容与话术检查</span>
          {statusTag(review, reviewing, stale)}
        </Space>
      }
      extra={reviewButton}
    >
      {reviewing && !review ? (
        <div className={styles.stateBox}>
          <Spin size="small" />
          <Text type="secondary">正在检查稿件内容与宣传话术...</Text>
        </div>
      ) : !review ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未检查这篇稿件" />
      ) : (
        <Space direction="vertical" size={12} className={styles.fullWidth}>
          {stale && <Alert message="稿件内容已修改，请重新检查" type="warning" showIcon />}
          {review.status === 'failed' && (
            <Alert
              message="稿件检查失败"
              description={review.error || review.summary}
              type="error"
              showIcon
            />
          )}
          {review.status === 'partial' && (
            <Alert message="检查部分完成" description={review.summary} type="warning" showIcon />
          )}
          {review.status !== 'failed' &&
            !review.fact_check_available &&
            review.status !== 'partial' && (
              <Alert message="事实检查不完整：缺少原文内容" type="warning" showIcon />
            )}

          <div className={styles.counts}>
            {(['high', 'medium', 'low'] as DraftReviewSeverity[]).map((severity) => (
              <Tag key={severity} color={SEVERITY_META[severity].color}>
                {SEVERITY_META[severity].label} {review.counts[severity] ?? 0}
              </Tag>
            ))}
          </div>

          {review.status !== 'failed' && review.issues.length === 0 ? (
            <Alert message={review.summary || '未发现需要修改的问题'} type="success" showIcon />
          ) : review.issues.length > 0 ? (
            <>
              <Segmented
                block
                size="small"
                value={filter}
                onChange={(value) => setFilter(value as ReviewFilter)}
                options={[
                  { label: `全部 (${review.issues.length})`, value: 'all' },
                  {
                    label: `事实内容 (${review.issues.filter((item) => FACT_CATEGORIES.has(item.category)).length})`,
                    value: 'fact',
                  },
                  {
                    label: `宣传话术 (${review.issues.filter((item) => !FACT_CATEGORIES.has(item.category)).length})`,
                    value: 'wording',
                  },
                ]}
              />
              <div className={styles.issueList}>
                {issues.length > 0 ? (
                  issues.map((issue) => <ReviewIssueItem key={issue.issue_id} issue={issue} />)
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="该分组没有问题" />
                )}
              </div>
            </>
          ) : null}
        </Space>
      )}
    </Card>
  );
}
