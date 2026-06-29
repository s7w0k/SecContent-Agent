/**
 * 统计卡片组件
 *
 * 展示仪表盘顶部 4 张核心统计指标卡片。
 *
 * Props:
 *   stats: StatsData | null  — 统计数据
 *   loading: boolean          — 加载态
 *   reportCount: number       — 已生成报道数
 */

import { Card, Col, Row, Skeleton, Statistic } from "antd";
import {
  FileTextOutlined,
  SafetyCertificateOutlined,
  StarOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import type { StatsData } from "../types";

interface StatsCardsProps {
  stats: StatsData | null;
  loading: boolean;
  reportCount: number;
}

export default function StatsCards({ stats, loading, reportCount }: StatsCardsProps) {
  if (loading) {
    return (
      <Row gutter={16} style={{ marginBottom: 24 }}>
        {[1, 2, 3, 4].map((i) => (
          <Col xs={24} sm={12} lg={6} key={i}>
            <Card>
              <Skeleton active paragraph={{ rows: 1 }} />
            </Card>
          </Col>
        ))}
      </Row>
    );
  }

  const total = stats?.total_articles ?? 0;
  const aiCount = stats?.ai_security_count ?? 0;
  const highValue = stats?.high_value_count ?? 0;

  const cards = [
    {
      title: "总文章数",
      value: total,
      icon: <FileTextOutlined />,
      color: "#1677ff",
    },
    {
      title: "AI 安全相关",
      value: aiCount,
      icon: <SafetyCertificateOutlined />,
      color: "#52c41a",
      suffix: total > 0 ? `${((aiCount / total) * 100).toFixed(0)}%` : undefined,
    },
    {
      title: "高价值文章 (≥140)",
      value: highValue,
      icon: <StarOutlined />,
      color: "#fa8c16",
      suffix: total > 0 ? `${((highValue / total) * 100).toFixed(0)}%` : undefined,
    },
    {
      title: "已生成报道",
      value: reportCount,
      icon: <ThunderboltOutlined />,
      color: "#722ed1",
    },
  ];

  return (
    <Row gutter={16} style={{ marginBottom: 24 }}>
      {cards.map((card) => (
        <Col xs={24} sm={12} lg={6} key={card.title}>
          <Card>
            <Statistic
              title={card.title}
              value={card.value}
              suffix={card.suffix}
              prefix={card.icon && <span style={{ color: card.color }}>{card.icon}</span>}
              valueStyle={{ color: card.color }}
            />
          </Card>
        </Col>
      ))}
    </Row>
  );
}
