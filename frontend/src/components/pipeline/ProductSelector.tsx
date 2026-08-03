/**
 * 产品选择器 - 可复用的产品多选组件
 */
import { Select, Tag } from 'antd';
import type { ProductCatalogItem } from '../../types';

interface ProductSelectorProps {
  products: ProductCatalogItem[];
  value?: string[];
  onChange?: (value: string[]) => void;
  maxCount?: number;
  disabled?: boolean;
  placeholder?: string;
}

export default function ProductSelector({
  products,
  value = [],
  onChange,
  maxCount = 5,
  disabled = false,
  placeholder = '请选择产品',
}: ProductSelectorProps) {
  return (
    <Select
      mode="multiple"
      style={{ width: '100%' }}
      value={value}
      onChange={onChange}
      maxCount={maxCount}
      disabled={disabled}
      placeholder={placeholder}
      optionRender={(option) => (
        <span>
          {option.label}
          {typeof option.data === 'object' && option.data?.published === false && (
            <Tag color="orange" style={{ marginInlineStart: 4 }}>
              未发布
            </Tag>
          )}
        </span>
      )}
    >
      {products.map((p) => (
        <Select.Option key={p.product_id} value={p.product_id} data={p}>
          {p.name}
        </Select.Option>
      ))}
    </Select>
  );
}
