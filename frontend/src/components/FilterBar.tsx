/**
 * 筛选栏组件
 *
 * 提供来源、分类、最低分、关键词、高价值等筛选条件，
 * 变更后通过 onChange 回调通知父组件重新加载数据。
 *
 * Props:
 *   value: FilterValues — 当前筛选值
 *   onChange: (values: FilterValues) => void
 *   categories: string[] — 可选分类列表（从数据中提取）
 */

import { ClearOutlined, SearchOutlined } from '@ant-design/icons';
import { Button, Col, Input, InputNumber, Row, Select, Space } from 'antd';
import { useCallback } from 'react';
import type { FilterValues, SourceType } from '../types';

const SOURCE_OPTIONS: { value: SourceType | ''; label: string }[] = [
  { value: '', label: '全部来源' },
  { value: 'overseas_news', label: '海外新闻' },
  { value: 'wechat_mp', label: '微信公众号' },
  { value: 'paper', label: '论文' },
  { value: 'user_upload', label: '用户上传' },
];

interface FilterBarProps {
  value: FilterValues;
  onChange: (values: FilterValues) => void;
  categories: string[];
}

export default function FilterBar({ value, onChange, categories }: FilterBarProps) {
  const handleChange = useCallback(
    (key: keyof FilterValues, val: unknown) => {
      onChange({ ...value, [key]: val || undefined });
    },
    [value, onChange],
  );

  const handleReset = useCallback(() => {
    onChange({});
  }, [onChange]);

  const categoryOptions = [
    { value: '', label: '全部分类' },
    ...categories.map((c) => ({ value: c, label: c })),
  ];

  return (
    <Row gutter={[12, 12]} style={{ marginBottom: 16 }} align="middle">
      <Col xs={24} sm={6} md={4}>
        <Select
          style={{ width: '100%' }}
          placeholder="来源"
          value={value.source_type || ''}
          onChange={(v) => handleChange('source_type', v || undefined)}
          options={SOURCE_OPTIONS}
        />
      </Col>
      <Col xs={24} sm={6} md={4}>
        <Select
          style={{ width: '100%' }}
          placeholder="分类"
          value={value.category || ''}
          onChange={(v) => handleChange('category', v || undefined)}
          options={categoryOptions}
          showSearch
        />
      </Col>
      <Col xs={12} sm={6} md={3}>
        <InputNumber
          style={{ width: '100%' }}
          placeholder="最低分"
          min={0}
          max={200}
          value={value.min_score}
          onChange={(v) => handleChange('min_score', v)}
        />
      </Col>
      <Col xs={12} sm={6} md={4}>
        <Input
          placeholder="关键词搜索"
          prefix={<SearchOutlined />}
          value={value.keyword || ''}
          onChange={(e) => handleChange('keyword', e.target.value || undefined)}
          allowClear
        />
      </Col>
      <Col>
        <Space>
          <Button icon={<ClearOutlined />} onClick={handleReset}>
            重置
          </Button>
        </Space>
      </Col>
    </Row>
  );
}
