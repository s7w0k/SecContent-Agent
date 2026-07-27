/**
 * 知识库用途徽标
 *
 * 将知识库角色（knowledge_role）映射为带颜色的 Ant Design Tag，
 * 并展示对应的中文标签。
 */

import { Tag } from 'antd';
import { useMemo } from 'react';

const ROLE_COLOR_MAP: Record<string, string> = {
  entry_router: 'default',
  folder_router: 'blue',
  role_workflow: 'purple',
  product_map: 'geekblue',
  product_fact: 'green',
  market_brief: 'cyan',
  sales_brief: 'gold',
  shared_fact: 'orange',
  raw_source: 'gray',
  overseas: 'gray',
  maintenance_log: 'warning',
};

const ROLE_LABEL_MAP: Record<string, string> = {
  entry_router: '入口路由',
  folder_router: '目录路由',
  role_workflow: '角色工作流',
  product_map: '产品图谱',
  product_fact: '产品事实',
  market_brief: '市场简报',
  sales_brief: '销售简报',
  shared_fact: '共享事实',
  raw_source: '原始资料',
  overseas: '海外资料',
  maintenance_log: '维护日志',
};

interface KnowledgeUsageBadgeProps {
  role: string;
  /** 自定义标签（覆盖默认角色映射） */
  label?: string;
}

export default function KnowledgeUsageBadge({ role, label }: KnowledgeUsageBadgeProps) {
  const { color, text } = useMemo(() => {
    return {
      color: ROLE_COLOR_MAP[role] || 'default',
      text: label || ROLE_LABEL_MAP[role] || role,
    };
  }, [role, label]);

  return <Tag color={color}>{text}</Tag>;
}
