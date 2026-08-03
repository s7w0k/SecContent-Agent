/**
 * 生成偏好页面
 */
import {
  Button,
  Card,
  Col,
  Descriptions,
  Radio,
  Row,
  Select,
  Space,
  Switch,
  Typography,
  message,
} from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { generationPreferencesApi, productCatalogApi } from '../api/client';
import type { GenerationPreferences, ProductCatalogItem } from '../types';

const { Title, Paragraph, Text } = Typography;

export default function GenerationPreferencesPage() {
  const [prefs, setPrefs] = useState<GenerationPreferences | null>(null);
  const [products, setProducts] = useState<ProductCatalogItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [p, catalog] = await Promise.all([
        generationPreferencesApi.get(),
        productCatalogApi.list('draft'),
      ]);
      setPrefs(p);
      setProducts(catalog.items);
      setDirty(false);
    } catch {
      message.error('加载生成偏好失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleSave = async () => {
    if (!prefs) return;
    setSaving(true);
    try {
      const saved = await generationPreferencesApi.save({
        product_relevance_enabled: prefs.product_relevance_enabled,
        product_target_mode: prefs.product_target_mode,
        selected_product_ids: prefs.selected_product_ids,
        expected_version: prefs.version,
      });
      setPrefs(saved);
      setDirty(false);
      message.success('保存成功');
    } catch (error: unknown) {
      const err = error as { response?: { status?: number; data?: { detail?: string } } };
      if (err.response?.status === 409) {
        message.error('版本冲突，请重新加载');
        void load();
      } else {
        message.error(err.response?.data?.detail || '保存失败');
      }
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async () => {
    try {
      const d = await generationPreferencesApi.reset();
      setPrefs(d);
      setDirty(false);
      message.success('已恢复系统默认');
    } catch {
      message.error('恢复失败');
    }
  };

  if (!prefs) return null;

  return (
    <div style={{ padding: 24, background: '#f5f7fb', minHeight: 'calc(100vh - 64px)' }}>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Col>
          <Title level={4} style={{ margin: 0 }}>
            生成偏好
          </Title>
          <Paragraph type="secondary" style={{ margin: 0 }}>
            配置账号级默认的产品相关性和产品选择
          </Paragraph>
        </Col>
        <Col>
          <Space>
            <Button onClick={handleReset} disabled={loading}>
              恢复默认
            </Button>
            <Button type="primary" loading={saving} onClick={handleSave} disabled={!dirty}>
              保存
            </Button>
          </Space>
        </Col>
      </Row>

      <Card loading={loading} size="small" style={{ marginBottom: 16 }}>
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <div>
            <Space>
              <Switch
                checked={prefs.product_relevance_enabled}
                disabled={prefs.product_target_mode === 'none'}
                onChange={(checked) => {
                  setPrefs({ ...prefs, product_relevance_enabled: checked });
                  setDirty(true);
                }}
              />
              <Text>默认启用产品相关性</Text>
            </Space>
          </div>

          <div>
            <Text strong style={{ display: 'block', marginBottom: 8 }}>
              关联产品模式
            </Text>
            <Radio.Group
              value={prefs.product_target_mode}
              onChange={(e) => {
                const mode = e.target.value;
                setPrefs({
                  ...prefs,
                  product_target_mode: mode,
                  product_relevance_enabled:
                    mode === 'none' ? false : prefs.product_relevance_enabled,
                });
                setDirty(true);
              }}
            >
              <Radio value="none">不关联产品</Radio>
              <Radio value="auto">自动匹配</Radio>
              <Radio value="selected">指定产品</Radio>
            </Radio.Group>
          </div>

          {prefs.product_target_mode === 'selected' && (
            <div>
              <Text strong style={{ display: 'block', marginBottom: 8 }}>
                选择产品（最多 5 个）
              </Text>
              <Select
                mode="multiple"
                style={{ width: '100%' }}
                value={prefs.selected_product_ids}
                onChange={(value) => {
                  setPrefs({ ...prefs, selected_product_ids: value });
                  setDirty(true);
                }}
                maxCount={5}
              >
                {products.map((p) => (
                  <Select.Option key={p.product_id} value={p.product_id}>
                    {p.name}
                  </Select.Option>
                ))}
              </Select>
            </div>
          )}

          <Descriptions title="分数规则说明" size="small" column={1} bordered>
            <Descriptions.Item label="产品相关性开启">
              候选分 = 产品相关度 + 事件影响力，范围 0~200，阈值 {prefs.product_event_threshold}
            </Descriptions.Item>
            <Descriptions.Item label="产品相关性关闭">
              候选分 = 事件影响力，范围 0~100，阈值 {prefs.event_only_threshold}
            </Descriptions.Item>
          </Descriptions>
        </Space>
      </Card>
    </div>
  );
}
