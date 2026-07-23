/**
 * useActiveTasks - 页面重新挂载时从后端恢复进行中的任务
 *
 * 解决 Dashboard/PipelineControl 切换 Tab 卸载后状态丢失的问题。
 * 后端任务状态持久化在 MongoDB pipeline_tasks 集合，
 * 此 Hook 在挂载时查询未完成任务并返回。
 */

import { useEffect, useState } from 'react';
import { pipelineApi } from '../api/client';
import type { PipelineTask } from '../types';

export interface RestoredPipelineTask {
  id: string;
  key: string;
  label: string;
}

export interface RestoredDraftTask {
  taskId: string;
  articleHash: string;
}

interface UseActiveTasksResult {
  /** PipelineControl 中的批量任务（run-v2/classify-v2/score-v2/crawl/report） */
  pipelineTask: RestoredPipelineTask | null;
  /** Dashboard 中的单文章草稿任务（run-v2 + article_url_hash） */
  draftTask: RestoredDraftTask | null;
  loading: boolean;
}

const TASK_LABELS: Record<string, { key: string; label: string }> = {
  crawl: { key: 'crawl', label: '爬取+分类' },
  'classify-v2': { key: 'classify-v2', label: 'V2分类' },
  'score-v2': { key: 'score-v2', label: 'V2打分' },
  'run-v2': { key: 'run-v2', label: 'V2智能PR流水线' },
  report: { key: 'report', label: '仅报道' },
};

const ACTIVE_STATUSES = ['pending', 'running'];

export function useActiveTasks(): UseActiveTasksResult {
  const [pipelineTask, setPipelineTask] = useState<RestoredPipelineTask | null>(null);
  const [draftTask, setDraftTask] = useState<RestoredDraftTask | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const res = await pipelineApi.getTasks(1, 20);
        if (!active) return;
        const activeTasks = res.items.filter((t: PipelineTask) =>
          ACTIVE_STATUSES.includes(t.status),
        );
        if (activeTasks.length === 0) return;

        // 单文章任务（run-v2 + article_url_hash）-> draftTask
        const single = activeTasks.find(
          (t) => t.task_type === 'run-v2' && t.article_url_hash,
        );
        if (single) {
          setDraftTask({
            taskId: single.task_id,
            articleHash: single.article_url_hash!,
          });
        }

        // 批量任务（无 article_url_hash）-> pipelineTask
        const batch = activeTasks.find((t) => !t.article_url_hash);
        if (batch) {
          const meta = TASK_LABELS[batch.task_type] || {
            key: batch.task_type,
            label: batch.task_type,
          };
          setPipelineTask({ id: batch.task_id, key: meta.key, label: meta.label });
        }
      } catch {
        // 静默失败，不阻塞页面加载
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  return { pipelineTask, draftTask, loading };
}
