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
  onRefresh: () => void;
}

export default function PipelineControl({ onComplete, onRefresh }: PipelineControlProps) {
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
  const handleCrawl = useCallback(() => trigger("crawl", "爬取+分类", 1), [trigger]);
  const handleCrawlOverseas = useCallback(async () => {
    setRunning(true);
    message.loading({ content: "海外新闻爬取中，预计 1-2 分钟...", key: "overseas", duration: 0 });
    try {
      const res = await api.crawlOverseas(1);
      message.success({ content: `海外新闻: ${res.saved} 篇入库 (共 ${res.total || 0} 篇)`, key: "overseas", duration: 4 });
      onComplete();
    } catch (e: any) {
      message.error({ content: `海外爬取失败: ${e?.message || ""}`, key: "overseas" });
    } finally {
      setRunning(false);
    }
  }, [onComplete]);
  const handleCrawlWewe = useCallback(async () => {
    setRunning(true);
    message.loading({ content: "公众号爬取中...", key: "wewe", duration: 0 });
    try {
      const res = await api.crawlWewe();
      message.success({ content: `公众号: ${res.saved} 篇入库`, key: "wewe", duration: 4 });
      onComplete();
    } catch (e: any) {
      message.error({ content: `公众号爬取失败: ${e?.message || ""}`, key: "wewe" });
    } finally {
      setRunning(false);
    }
  }, [onComplete]);
  const handleScoreV2 = useCallback(async () => {
    try {
      setRunning(true);
      message.loading({ content: "V2打分中...", key: "scoreV2", duration: 0 });
      const res = await api.scoreV2();
      message.success({
        content: `V2打分: ${res.scored} 篇 (${res.candidates} 篇达标≥80)`,
        key: "scoreV2", duration: 4,
      });
      onComplete();
      onRefresh();
    } catch (e: unknown) {
      message.error({ content: `V2打分失败`, key: "scoreV2" });
    } finally {
      setRunning(false);
    }
  }, [onComplete, onRefresh]);
  const handleReport = useCallback(() => trigger("report", "报道"), [trigger]);
  const handleClassifyV2 = useCallback(async () => {
    try {
      setRunning(true);
      message.loading({ content: "6分类中...", key: "classifyV2", duration: 0 });
      const res = await api.classifyV2();
      message.success({
        content: `6分类完成: ${res.classified} 篇 (${res.summary ? Object.entries(res.summary).map(([k, v]) => `${k}:${v}`).join(", ") : ""})`,
        key: "classifyV2",
        duration: 5,
      });
      onComplete();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "未知错误";
      message.error({ content: `6分类失败: ${msg}`, key: "classifyV2" });
    } finally {
      setRunning(false);
    }
  }, [onComplete]);
  const handleRunV2 = useCallback(async () => {
    try {
      setRunning(true);
      message.loading({ content: "V2智能PR流水线运行中...", key: "pipelineV2", duration: 0 });
      const res = await api.runV2(1);
      message.success({
        content: `V2流水线完成: 分类${res.state?.classified_v2_count || 0} 打分${res.state?.scored_v2_count || 0} 草稿${res.state?.draft_count || 0}`,
        key: "pipelineV2", duration: 5,
      });
      onComplete();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "未知错误";
      message.error({ content: `V2流水线失败: ${msg}`, key: "pipelineV2" });
    } finally {
      setRunning(false);
    }
  }, [onComplete]);

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
          爬取+分类
        </Button>
        <Button onClick={handleCrawlOverseas} disabled={running} loading={running}>
          海外新闻
        </Button>
        <Button onClick={handleCrawlWewe} disabled={running} loading={running}>
          公众号
        </Button>
        <Button
          icon={<ExperimentOutlined />}
          onClick={handleScoreV2}
          disabled={running}
        >
          V2打分
        </Button>
        <Button
          icon={<ExperimentOutlined />}
          onClick={handleClassifyV2}
          disabled={running}
        >
          V2分类
        </Button>
        <Button
          type="primary"
          icon={<PlayCircleOutlined />}
          onClick={handleRunV2}
          loading={running}
        >
          智能PR流水线
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
