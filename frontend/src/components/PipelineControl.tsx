/**
 * 流水线控制面板
 *
 * 提供流水线触发按钮、进度 Steps、状态显示。
 * 运行时每 2s 轮询状态，完成后自动通知父组件刷新数据。
 *
 * Props:
 *   onComplete: () => void  — 流水线完成后的回调（刷新数据）
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  message,
  Space,
  Steps,
  Tag,
  Typography,
} from "antd";
import {
  PlayCircleOutlined,
  SyncOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  CloudDownloadOutlined,
  ExperimentOutlined,
  FileTextOutlined,
} from "@ant-design/icons";
import api from "../api/client";
import type { PipelineState, PipelineStatus } from "../types";

const { Text } = Typography;
const POLL_INTERVAL_MS = 2000;

const PHASE_STEPS = [
  { title: "爬取", icon: <CloudDownloadOutlined />, key: "crawled_count" as const },
  { title: "分类", icon: <ExperimentOutlined />, key: "classified_count" as const },
  { title: "打分", icon: <SyncOutlined />, key: "scored_count" as const },
  { title: "报道", icon: <FileTextOutlined />, key: "report_count" as const },
];

interface PipelineControlProps {
  onComplete: () => void;
}

export default function PipelineControl({ onComplete }: PipelineControlProps) {
  const [status, setStatus] = useState<PipelineStatus>("idle");
  const [state, setState] = useState<PipelineState | null>(null);
  const [running, setRunning] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
  }, []);

  const pollStatus = useCallback(async () => {
    try {
      const res = await api.getStatus();
      const s = res.status as PipelineStatus;
      setStatus(s);
      setErrors(res.errors || []);

      if (res.state && Object.keys(res.state).length > 0) {
        setState(res.state as PipelineState);
      }

      if (s === "completed" || s === "failed" || s === "cancelled") {
        stopPolling();
        setRunning(false);
        if (s === "completed") {
          message.success("流水线执行完成");
          onComplete();
        } else if (s === "failed") {
          message.error(`流水线执行失败: ${res.errors?.join(", ") || "未知错误"}`);
        }
      }
    } catch {
      // 轮询失败不影响继续尝试
    }
  }, [stopPolling, onComplete]);

  const startPolling = useCallback(() => {
    stopPolling();
    pollStatus(); // 立即查询一次
    pollingRef.current = setInterval(pollStatus, POLL_INTERVAL_MS);
  }, [stopPolling, pollStatus]);

  // 清理定时器
  useEffect(() => {
    return () => stopPolling();
  }, [stopPolling]);

  // ── 触发操作 ──────────────────────────────────────────────

  const trigger = useCallback(
    async (action: "run" | "crawl" | "score" | "report", label: string, days?: number) => {
      console.log(`[Pipeline] Triggering ${action}...`);
      setRunning(true);
      setErrors([]);
      setStatus("running");
      message.loading({ content: `${label}中...`, key: "pipeline", duration: 0 });

      try {
        if (action === "run") await api.run(days || 1);
        else if (action === "crawl") await api.crawl(days || 1);
        else if (action === "score") await api.score();
        else if (action === "report") await api.report();
        console.log(`[Pipeline] ${action} triggered successfully`);
        message.success({ content: `${label}已触发`, key: "pipeline", duration: 2 });
        startPolling();
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : "未知错误";
        console.error(`[Pipeline] ${action} failed:`, e);
        message.error({ content: `${label}失败: ${msg}`, key: "pipeline" });
        setRunning(false);
        setStatus("failed");
      }
    },
    [startPolling],
  );

  const handleRunFull = useCallback(() => trigger("run", "全流程", 1), [trigger]);
  const handleCrawl = useCallback(() => trigger("crawl", "爬取", 1), [trigger]);
  const handleScore = useCallback(() => trigger("score", "打分"), [trigger]);
  const handleReport = useCallback(() => trigger("report", "报道"), [trigger]);

  // ── 当前阶段索引 ──────────────────────────────────────────

  const phaseIndex = state
    ? PHASE_STEPS.findIndex((s) => {
        if (state.report_count > 0) return s.key === "report_count";
        if (state.scored_count > 0) return s.key === "scored_count";
        if (state.classified_count > 0) return s.key === "classified_count";
        if (state.crawled_count > 0) return s.key === "crawled_count";
        return false;
      })
    : -1;

  // ── 状态标签 ──────────────────────────────────────────────

  const statusTag = () => {
    switch (status) {
      case "idle":
        return <Tag color="default">● 空闲</Tag>;
      case "running":
        return <Tag color="processing" icon={<SyncOutlined spin />}>运行中</Tag>;
      case "completed":
        return <Tag color="success" icon={<CheckCircleOutlined />}>完成</Tag>;
      case "failed":
        return <Tag color="error" icon={<CloseCircleOutlined />}>失败</Tag>;
      case "cancelled":
        return <Tag color="warning">已取消</Tag>;
    }
  };

  return (
    <Card
      title={
        <Space>
          <Text strong>流水线控制</Text>
          {statusTag()}
        </Space>
      }
      style={{ marginBottom: 16 }}
    >
      {/* 触发按钮 */}
      <Space wrap style={{ marginBottom: 16 }}>
        <Button
          type="primary"
          icon={<PlayCircleOutlined />}
          onClick={handleRunFull}
          loading={running}
        >
          全流程
        </Button>
        <Button
          icon={<CloudDownloadOutlined />}
          onClick={handleCrawl}
          disabled={running}
        >
          仅爬取
        </Button>
        <Button
          icon={<ExperimentOutlined />}
          onClick={handleScore}
          disabled={running}
        >
          仅打分
        </Button>
        <Button
          icon={<FileTextOutlined />}
          onClick={handleReport}
          disabled={running}
        >
          仅报道
        </Button>
      </Space>

      {/* 进度 Steps */}
      {state && (
        <Steps
          current={phaseIndex >= 0 ? phaseIndex : 0}
          status={
            status === "failed"
              ? "error"
              : status === "completed"
                ? "finish"
                : "process"
          }
          size="small"
          items={PHASE_STEPS.map((s) => ({
            title: s.title,
            description: `${state[s.key]} 篇`,
          }))}
        />
      )}

      {/* 首次使用提示 */}
      {!state && status === "idle" && (
        <Text type="secondary">点击"全流程"开始爬取、分类、打分和报道生成</Text>
      )}

      {/* 错误展示 */}
      {errors.length > 0 && (
        <Alert
          type="error"
          message="执行错误"
          description={errors.map((e, i) => <div key={i}>{e}</div>)}
          showIcon
          closable
          style={{ marginTop: 12 }}
        />
      )}
    </Card>
  );
}
