/**
 * 知识库草稿校验与预览面板（K.5）
 *
 * 提供三个标签页：
 *   1. 校验 — 运行草稿校验，展示错误/警告与加载器文件统计
 *   2. Prompt 预览 — 对比新旧评分 Prompt，展示哈希与字符数变化
 *   3. 试打分 — 使用测试文章对比新旧评分（会产生 LLM 费用）
 */

import {
  ExperimentOutlined,
  PlayCircleOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons';
import { Alert, Button, Col, Input, Row, Space, Spin, Statistic, Tabs, Tag, Typography, message } from 'antd';
import { useState } from 'react';
import { knowledgeAdminApi } from '../api/client';
import type {
  KnowledgePreviewArticle,
  KnowledgePromptPreview,
  KnowledgeScorePreview,
  KnowledgeValidationResult,
} from '../types';

const { Text, Title } = Typography;

interface KnowledgePreviewPanelProps {
  draftId: string;
  relativePath: string;
}

interface HttpLikeError {
  response?: {
    status?: number;
    data?: {
      detail?: string;
    };
  };
  message?: string;
}

function getErrorMessage(error: unknown, fallback: string) {
  const maybeError = error as HttpLikeError;
  return maybeError.response?.data?.detail || maybeError.message || fallback;
}

const PROMPT_PRE_STYLE: React.CSSProperties = {
  background: '#fafafa',
  border: '1px solid #f0f0f0',
  borderRadius: 4,
  padding: 12,
  maxHeight: 320,
  overflow: 'auto',
  fontSize: 12,
  whiteSpace: 'pre-wrap',
  margin: 0,
};

export default function KnowledgePreviewPanel({ draftId, relativePath }: KnowledgePreviewPanelProps) {
  // ── 校验 ──
  const [validating, setValidating] = useState(false);
  const [validationResult, setValidationResult] = useState<KnowledgeValidationResult | null>(null);

  // ── Prompt 预览 ──
  const [previewingPrompt, setPreviewingPrompt] = useState(false);
  const [promptPreview, setPromptPreview] = useState<KnowledgePromptPreview | null>(null);

  // ── 试打分 ──
  const [previewingScore, setPreviewingScore] = useState(false);
  const [scorePreview, setScorePreview] = useState<KnowledgeScorePreview | null>(null);
  const [article, setArticle] = useState<KnowledgePreviewArticle>({
    title: '',
    source: '',
    category_v2: '',
    summary_cn: '',
    content_md: '',
  });

  const handleValidate = async () => {
    setValidating(true);
    try {
      const result = await knowledgeAdminApi.validateDraft(draftId);
      setValidationResult(result);
      if (result.status === 'passed') {
        message.success('校验通过');
      } else {
        message.warning('校验未通过，请查看错误信息');
      }
    } catch (err) {
      message.error(getErrorMessage(err, '校验失败'));
    } finally {
      setValidating(false);
    }
  };

  const handlePreviewPrompt = async () => {
    setPreviewingPrompt(true);
    try {
      const result = await knowledgeAdminApi.previewPrompt(draftId);
      setPromptPreview(result);
      message.success('Prompt 预览已生成');
    } catch (err) {
      message.error(getErrorMessage(err, 'Prompt 预览失败'));
    } finally {
      setPreviewingPrompt(false);
    }
  };

  const handlePreviewScore = async () => {
    if (!article.title.trim() || !article.content_md.trim()) {
      message.warning('请填写文章标题和正文');
      return;
    }
    setPreviewingScore(true);
    try {
      const result = await knowledgeAdminApi.previewScore(draftId, article);
      setScorePreview(result);
      message.success('试打分完成');
    } catch (err) {
      message.error(getErrorMessage(err, '试打分失败'));
    } finally {
      setPreviewingScore(false);
    }
  };

  // ── 校验标签页 ──
  const validateTab = (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Button
        type="primary"
        icon={<SafetyCertificateOutlined />}
        loading={validating}
        onClick={handleValidate}
      >
        运行校验
      </Button>
      {validating && !validationResult && <Spin size="small" />}
      {validationResult && (
        <>
          <Tag color={validationResult.status === 'passed' ? 'success' : 'error'} style={{ marginInlineStart: 0 }}>
            {validationResult.status === 'passed' ? '校验通过' : '校验失败'}
          </Tag>
          <Row gutter={16}>
            <Col span={12}>
              <Statistic title="加载文件数" value={validationResult.loader_file_count} />
            </Col>
            <Col span={12}>
              <Statistic title="评分相关文件数" value={validationResult.loader_relevant_count} />
            </Col>
          </Row>
          {validationResult.errors.length > 0 && (
            <Alert
              type="error"
              showIcon
              message="错误"
              description={
                <ul style={{ margin: 0, paddingLeft: 20 }}>
                  {validationResult.errors.map((err, idx) => (
                    <li key={`err-${idx}`}>{err}</li>
                  ))}
                </ul>
              }
            />
          )}
          {validationResult.warnings.length > 0 && (
            <Alert
              type="warning"
              showIcon
              message="警告"
              description={
                <ul style={{ margin: 0, paddingLeft: 20 }}>
                  {validationResult.warnings.map((warn, idx) => (
                    <li key={`warn-${idx}`}>{warn}</li>
                  ))}
                </ul>
              }
            />
          )}
        </>
      )}
    </Space>
  );

  // ── Prompt 预览标签页 ──
  const promptTab = (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Button
        type="primary"
        icon={<ExperimentOutlined />}
        loading={previewingPrompt}
        onClick={handlePreviewPrompt}
      >
        生成 Prompt 预览
      </Button>
      {previewingPrompt && !promptPreview && <Spin size="small" />}
      {promptPreview && (
        <>
          <Row gutter={16} align="middle">
            <Col span={6}>
              <Statistic title="旧 Prompt 字符数" value={promptPreview.char_count_old} />
            </Col>
            <Col span={6}>
              <Statistic title="新 Prompt 字符数" value={promptPreview.char_count_new} />
            </Col>
            <Col span={6}>
              <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>
                旧哈希
              </Text>
              <Text code style={{ fontSize: 12 }}>
                {promptPreview.old_hash?.slice(0, 12) || '-'}
              </Text>
            </Col>
            <Col span={6}>
              <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>
                新哈希
              </Text>
              <Text code style={{ fontSize: 12 }}>
                {promptPreview.new_hash?.slice(0, 12) || '-'}
              </Text>
            </Col>
          </Row>
          <Space wrap>
            <Tag color={promptPreview.prompt_changed ? 'orange' : 'green'}>
              {promptPreview.prompt_changed ? 'Prompt 已变化' : 'Prompt 未变化'}
            </Tag>
            <Tag color={promptPreview.file_in_prompt ? 'blue' : 'default'}>
              {promptPreview.file_in_prompt ? '当前文件在 Prompt 中' : '当前文件不在 Prompt 中'}
            </Tag>
          </Space>
          <Row gutter={12}>
            <Col span={12}>
              <Text strong>旧 Prompt</Text>
              <pre style={PROMPT_PRE_STYLE} aria-label="旧 Prompt 内容">
                {promptPreview.old_prompt || '(空)'}
              </pre>
            </Col>
            <Col span={12}>
              <Text strong>新 Prompt</Text>
              <pre style={PROMPT_PRE_STYLE} aria-label="新 Prompt 内容">
                {promptPreview.new_prompt || '(空)'}
              </pre>
            </Col>
          </Row>
        </>
      )}
    </Space>
  );

  // ── 试打分标签页 ──
  const scoreTab = (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Alert
        type="warning"
        showIcon
        message="试打分会调用 LLM，产生模型费用"
        description="每次试打分都会向大模型发送请求，请按需使用。"
      />
      <div>
        <Row gutter={12}>
          <Col span={12}>
            <Text strong style={{ display: 'block', marginBottom: 4 }}>
              标题 *
            </Text>
            <Input
              aria-label="文章标题"
              value={article.title}
              onChange={(e) => setArticle({ ...article, title: e.target.value })}
              placeholder="文章标题"
            />
          </Col>
          <Col span={6}>
            <Text strong style={{ display: 'block', marginBottom: 4 }}>
              来源
            </Text>
            <Input
              aria-label="文章来源"
              value={article.source}
              onChange={(e) => setArticle({ ...article, source: e.target.value })}
              placeholder="来源"
            />
          </Col>
          <Col span={6}>
            <Text strong style={{ display: 'block', marginBottom: 4 }}>
              分类
            </Text>
            <Input
              aria-label="文章分类"
              value={article.category_v2}
              onChange={(e) => setArticle({ ...article, category_v2: e.target.value })}
              placeholder="分类"
            />
          </Col>
        </Row>
      </div>
      <div>
        <Text strong style={{ display: 'block', marginBottom: 4 }}>
          摘要
        </Text>
        <Input.TextArea
          aria-label="文章摘要"
          value={article.summary_cn}
          onChange={(e) => setArticle({ ...article, summary_cn: e.target.value })}
          placeholder="中文摘要"
          rows={2}
        />
      </div>
      <div>
        <Text strong style={{ display: 'block', marginBottom: 4 }}>
          正文 *
        </Text>
        <Input.TextArea
          aria-label="文章正文"
          value={article.content_md}
          onChange={(e) => setArticle({ ...article, content_md: e.target.value })}
          placeholder="Markdown 正文"
          rows={6}
        />
      </div>
      <Button
        type="primary"
        icon={<PlayCircleOutlined />}
        loading={previewingScore}
        onClick={handlePreviewScore}
        disabled={!article.title.trim() || !article.content_md.trim()}
      >
        运行试打分
      </Button>
      {previewingScore && <Spin size="small" />}
      {scorePreview && (
        <>
          <Tag color={scorePreview.score_changed ? 'orange' : 'green'} style={{ marginInlineStart: 0 }}>
            {scorePreview.score_changed ? '分数已变化' : '分数未变化'}
          </Tag>
          <Row gutter={24}>
            <Col span={12}>
              <Title level={5}>旧分数</Title>
              {scorePreview.old_score ? (
                <Row gutter={8}>
                  <Col span={8}>
                    <Statistic
                      title="产品相关性"
                      value={scorePreview.old_score.product_relevance}
                    />
                  </Col>
                  <Col span={8}>
                    <Statistic
                      title="事件影响力"
                      value={scorePreview.old_score.event_impact}
                    />
                  </Col>
                  <Col span={8}>
                    <Statistic title="PR 总分" value={scorePreview.old_score.pr_total_score} />
                  </Col>
                </Row>
              ) : (
                <Text type="secondary">无旧分数</Text>
              )}
              {scorePreview.old_score?.error && (
                <Alert
                  type="error"
                  message={scorePreview.old_score.error}
                  style={{ marginTop: 8 }}
                />
              )}
            </Col>
            <Col span={12}>
              <Title level={5}>新分数</Title>
              {scorePreview.new_score ? (
                <Row gutter={8}>
                  <Col span={8}>
                    <Statistic
                      title="产品相关性"
                      value={scorePreview.new_score.product_relevance}
                    />
                  </Col>
                  <Col span={8}>
                    <Statistic
                      title="事件影响力"
                      value={scorePreview.new_score.event_impact}
                    />
                  </Col>
                  <Col span={8}>
                    <Statistic title="PR 总分" value={scorePreview.new_score.pr_total_score} />
                  </Col>
                </Row>
              ) : (
                <Text type="secondary">无新分数</Text>
              )}
              {scorePreview.new_score?.error && (
                <Alert
                  type="error"
                  message={scorePreview.new_score.error}
                  style={{ marginTop: 8 }}
                />
              )}
            </Col>
          </Row>
        </>
      )}
    </Space>
  );

  return (
    <div>
      {relativePath && (
        <div style={{ marginBottom: 12 }}>
          <Text type="secondary">草稿关联文件：</Text>
          <Text code>{relativePath}</Text>
        </div>
      )}
      <Tabs
        defaultActiveKey="validate"
        items={[
          {
            key: 'validate',
            label: '校验',
            children: validateTab,
          },
          {
            key: 'prompt',
            label: 'Prompt 预览',
            children: promptTab,
          },
          {
            key: 'score',
            label: '试打分',
            children: scoreTab,
          },
        ]}
      />
    </div>
  );
}
