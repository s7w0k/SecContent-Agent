/**
 * V2 PR 草稿查看器
 *
 * Modal 弹窗展示 V2 PR 流水线生成的 4 篇草稿，
 * 支持切换查看、复制和下载。
 */

import { CopyOutlined, DownloadOutlined } from '@ant-design/icons';
import { Button, Descriptions, Divider, Modal, Radio, Space, Tag, Typography, message } from 'antd';
import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import api from '../api/client';
import type { Article, DraftItem } from '../types';
import DraftFeedback from './DraftFeedback';

const { Paragraph } = Typography;

interface DraftViewerProps {
  article: Article | null;
  onClose: () => void;
}

export default function DraftViewer({ article, onClose }: DraftViewerProps) {
  const [index, setIndex] = useState(0);

  if (!article?.pr_drafts?.length) return null;

  const drafts: DraftItem[] = article.pr_drafts;
  const current = drafts[index] || drafts[0];
  const templates = [...new Set(drafts.map((d) => d.template))].join(', ');

  const handleCopy = () => {
    if (!current?.content_md) return;
    navigator.clipboard
      .writeText(current.content_md)
      .then(() => message.success('已复制'))
      .catch(() => message.error('复制失败'));
  };

  const handleDownload = () => {
    if (!current?.content_md) return;
    const blob = new Blob([current.content_md], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `PR-${current.template}-${current.index}.md`;
    a.click();
    URL.revokeObjectURL(url);
    void api
      .log({
        action: 'draft_download',
        target: {
          article_url_hash: article.url_hash,
          draft_index: index,
          template: current.template,
          template_id: current.template_id,
          template_key: current.template_key,
          template_version: current.template_version,
          template_name: current.template,
          perspective: current.perspective,
        },
        context: {
          article_title: article.title,
          category_v2: article.category_v2,
          pr_total_score: article.pr_total_score,
        },
      })
      .catch(() => undefined);
  };

  return (
    <Modal
      title={`PR 草稿 — ${article.title?.slice(0, 40)}...`}
      open
      onCancel={onClose}
      width={900}
      footer={
        <Space>
          <Button icon={<CopyOutlined />} onClick={handleCopy}>
            复制
          </Button>
          <Button icon={<DownloadOutlined />} onClick={handleDownload}>
            下载 .md
          </Button>
          <Button onClick={onClose}>关闭</Button>
        </Space>
      }
    >
      <Descriptions size="small" column={3} bordered style={{ marginBottom: 12 }}>
        <Descriptions.Item label="V2分类">
          <Tag color="red">{article.category_v2 || '-'}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="产品相关度">
          {article.product_relevance ?? '-'}/100
        </Descriptions.Item>
        <Descriptions.Item label="事件影响力">{article.event_impact ?? '-'}/100</Descriptions.Item>
        <Descriptions.Item label="V2综合分">
          <Tag color={(article.pr_total_score ?? 0) >= 80 ? 'red' : 'orange'}>
            {article.pr_total_score ?? '-'}
          </Tag>
        </Descriptions.Item>
        <Descriptions.Item label="模板">
          <Tag>{templates}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="草稿数">{drafts.length} 篇</Descriptions.Item>
      </Descriptions>

      <Radio.Group
        value={index}
        onChange={(e) => setIndex(e.target.value)}
        style={{ marginBottom: 8 }}
        buttonStyle="solid"
        size="small"
      >
        {drafts.map((d, i) => (
          <Radio.Button key={i} value={i}>
            {d.template}-{d.index}
          </Radio.Button>
        ))}
      </Radio.Group>
      <Tag style={{ marginLeft: 8 }}>{current.perspective}</Tag>

      <Divider />

      <div style={{ maxHeight: '55vh', overflow: 'auto', padding: '8px 0' }}>
        {current?.content_md ? (
          <ReactMarkdown>{current.content_md}</ReactMarkdown>
        ) : (
          <Paragraph type="secondary">草稿内容不可用</Paragraph>
        )}
      </div>
      <DraftFeedback
        articleUrlHash={article.url_hash}
        draftIndex={index}
        template={current.template}
        perspective={current.perspective}
        initialRating={current.feedback_summary?.last_rating}
      />
    </Modal>
  );
}
