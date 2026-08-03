/**
 * 配置摘要标签 - 显示产品模式和评分模式
 */
import { Tag, Tooltip } from 'antd';

interface ConfigSummaryTagProps {
  productTargetMode?: string;
  scoreMode?: string;
  productRelevanceEnabled?: boolean;
  selectedProductIds?: string[];
  resolvedProducts?: string[];
}

const MODE_LABELS: Record<string, string> = {
  none: '不关联',
  auto: '自动',
  selected: '指定',
};

const MODE_COLORS: Record<string, string> = {
  none: 'default',
  auto: 'blue',
  selected: 'green',
};

export default function ConfigSummaryTag({
  productTargetMode,
  scoreMode,
  productRelevanceEnabled = true,
  selectedProductIds = [],
  resolvedProducts = [],
}: ConfigSummaryTagProps) {
  if (!productTargetMode) return null;

  const modeLabel = MODE_LABELS[productTargetMode] || productTargetMode;
  const modeColor = MODE_COLORS[productTargetMode] || 'default';
  const productCount = resolvedProducts.length || selectedProductIds.length;

  const tooltipContent = [
    `产品模式: ${modeLabel}`,
    `评分模式: ${scoreMode === 'event_only' ? '事件分(0-100)' : '产品+事件(0-200)'}`,
    productRelevanceEnabled ? '产品相关性: 开启' : '产品相关性: 关闭',
    productCount > 0 ? `关联产品: ${productCount} 个` : '无关联产品',
  ].join('\n');

  return (
    <Tooltip title={tooltipContent}>
      <Tag color={modeColor} style={{ cursor: 'help' }}>
        {modeLabel}
        {productCount > 0 ? `·${productCount}` : ''}
        {!productRelevanceEnabled && productTargetMode !== 'none' ? '·无PR' : ''}
      </Tag>
    </Tooltip>
  );
}
