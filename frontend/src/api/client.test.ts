/**
 * API Client — 单元测试
 *
 * 运行:
 *   cd frontend && npx vitest run src/api/client.test.ts
 */

import axios from 'axios';
import type { AxiosInstance } from 'axios';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Article, Report } from '../types';

// Mock axios
vi.mock('axios', () => {
  const mockGet = vi.fn();
  const mockPost = vi.fn();
  const mockPut = vi.fn();
  const mockDelete = vi.fn();
  const mockAxios = {
    create: vi.fn(() => ({
      get: mockGet,
      post: mockPost,
      put: mockPut,
      delete: mockDelete,
      interceptors: {
        response: { use: vi.fn() },
        request: { use: vi.fn() },
      },
    })),
  };
  return { default: mockAxios };
});

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockPut = vi.fn();
const mockDelete = vi.fn();
let requestInterceptor: ((config: { headers: Record<string, string> }) => unknown) | undefined;
let responseErrorInterceptor: ((error: unknown) => Promise<never>) | undefined;

// Re-create client with controlled mocks
let api: typeof import('./client').default;

async function setupApi() {
  // Clear the module cache to re-import with fresh mocks
  vi.resetModules();

  // Override the create mock
  (axios.create as ReturnType<typeof vi.fn>).mockReturnValue({
    get: mockGet,
    post: mockPost,
    put: mockPut,
    delete: mockDelete,
    interceptors: {
      response: {
        use: vi.fn((_success, error) => {
          responseErrorInterceptor = error;
        }),
      },
      request: {
        use: vi.fn((success) => {
          requestInterceptor = success;
        }),
      },
    },
  } as unknown as AxiosInstance);

  const mod = await import('./client');
  api = mod.default;
}

beforeEach(() => {
  window.localStorage.clear();
  requestInterceptor = undefined;
  responseErrorInterceptor = undefined;
});

// ═══════════════════════════════════════════════════════════
// Authentication API + interceptors
// ═══════════════════════════════════════════════════════════

describe('Authentication API', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await setupApi();
  });

  it('request interceptor adds bearer token', () => {
    window.localStorage.setItem('access_token', 'jwt-token');
    const config = { headers: {} as Record<string, string> };

    requestInterceptor?.(config);

    expect(config.headers.Authorization).toBe('Bearer jwt-token');
  });

  it('401 response clears token and emits unauthorized event', async () => {
    window.localStorage.setItem('access_token', 'expired-token');
    const listener = vi.fn();
    window.addEventListener('auth:unauthorized', listener);
    const error = {
      response: { status: 401, data: { detail: 'expired' } },
      config: { url: '/profile/style' },
      message: 'Unauthorized',
    };

    await expect(responseErrorInterceptor?.(error)).rejects.toBe(error);

    expect(window.localStorage.getItem('access_token')).toBeNull();
    expect(listener).toHaveBeenCalledOnce();
    window.removeEventListener('auth:unauthorized', listener);
  });

  it('login, register, me and deleteAccount call auth endpoints', async () => {
    const user = {
      user_id: 'user-a',
      username: 'alice',
      display_name: 'Alice',
      created_at: '2026-07-11T00:00:00Z',
    };
    mockPost
      .mockResolvedValueOnce({ data: { data: user } })
      .mockResolvedValueOnce({ data: { data: { access_token: 'token', user } } });
    mockGet.mockResolvedValueOnce({ data: { data: user } });
    mockDelete.mockResolvedValueOnce({ data: { data: { message: 'deleted' } } });
    const { authApi } = await import('./client');

    await authApi.register({ username: 'alice', password: 'secret1' });
    const login = await authApi.login({ username: 'alice', password: 'secret1' });
    const current = await authApi.me();
    await authApi.deleteAccount('secret1');

    expect(mockPost).toHaveBeenNthCalledWith(1, '/auth/register', {
      username: 'alice',
      password: 'secret1',
    });
    expect(mockPost).toHaveBeenNthCalledWith(2, '/auth/login', {
      username: 'alice',
      password: 'secret1',
    });
    expect(mockGet).toHaveBeenCalledWith('/auth/me');
    expect(mockDelete).toHaveBeenCalledWith('/auth/account', {
      data: { password: 'secret1' },
    });
    expect(login.access_token).toBe('token');
    expect(current.user_id).toBe('user-a');
  });

  it('buildSSEUrl appends token and query parameters', async () => {
    window.localStorage.setItem('access_token', 'sse-token');
    const { buildSSEUrl } = await import('./client');

    const url = new URL(buildSSEUrl('/chat/ask_stream', { draft_index: 2 }));

    expect(url.pathname).toBe('/api/chat/ask_stream');
    expect(url.searchParams.get('token')).toBe('sse-token');
    expect(url.searchParams.get('draft_index')).toBe('2');
  });
});

// ═══════════════════════════════════════════════════════════
// Dashboard API
// ═══════════════════════════════════════════════════════════

describe('Dashboard API', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await setupApi();
  });

  it('getArticles calls GET /articles with query params', async () => {
    const mockData = { items: [], total: 0, page: 1, page_size: 20, pages: 0 };
    mockGet.mockResolvedValueOnce({ data: mockData });

    const result = await api.getArticles({ page: 1, source_type: 'overseas_news' });
    expect(mockGet).toHaveBeenCalledWith('/articles', {
      params: { page: 1, source_type: 'overseas_news' },
    });
    expect(result).toEqual(mockData);
  });

  it('getArticles handles empty query', async () => {
    mockGet.mockResolvedValueOnce({
      data: { items: [], total: 0, page: 1, page_size: 20, pages: 0 },
    });
    await api.getArticles();
    expect(mockGet).toHaveBeenCalledWith('/articles', { params: {} });
  });

  it('uploadArticle sends multipart form data and unwraps the result', async () => {
    const response = {
      url_hash: 'hash-1',
      title: '上传文章',
      source_type: 'user_upload' as const,
      content_length: 120,
      message: '文章已入库',
    };
    mockPost.mockResolvedValueOnce({ data: { ok: true, data: response } });
    const file = new File(['content'], 'article.md', { type: 'text/markdown' });

    const result = await api.uploadArticle(file, ' 上传文章 ');

    expect(mockPost).toHaveBeenCalledWith('/upload/article', expect.any(FormData), {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
    const form = mockPost.mock.calls[0][1] as FormData;
    expect(form.get('file')).toBe(file);
    expect(form.get('title')).toBe('上传文章');
    expect(result).toEqual(response);
  });

  it('getArticle calls GET /articles/:hash', async () => {
    const article = { _id: '1', title: 'Test', url_hash: 'abc' } as Article;
    mockGet.mockResolvedValueOnce({ data: article });

    const result = await api.getArticle('abc');
    expect(mockGet).toHaveBeenCalledWith('/articles/abc');
    expect(result).toEqual(article);
  });

  it('getStats returns stats data', async () => {
    const stats = {
      total_articles: 42,
      ai_security_count: 10,
      high_value_count: 5,
      source_distribution: {},
      category_distribution: {},
      today_count: 3,
      today_ai_security_count: 2,
      today_high_value_count: 1,
    };
    mockGet.mockResolvedValueOnce({ data: stats });

    const result = await api.getStats();
    expect(mockGet).toHaveBeenCalledWith('/stats');
    expect(result.total_articles).toBe(42);
    expect(result.today_count).toBe(3);
  });

  it('getHotRanking sends defaults and unwraps items', async () => {
    const items = [
      {
        url_hash: 'hot-1',
        title: '热点文章',
        url: 'https://example.com/hot-1',
        pr_total_score: 188,
        category_v2: 'AI安全漏洞与攻击',
        added_at: '2026-07-21T08:30:00Z',
        source_type: 'overseas_news',
      },
    ];
    mockGet.mockResolvedValueOnce({
      data: { ok: true, data: { items, total: 1 } },
    });

    const result = await api.getHotRanking();

    expect(mockGet).toHaveBeenCalledWith('/articles/hot', {
      params: { limit: 10, category: 'all', date_range: '7d' },
    });
    expect(result).toEqual(items);
  });

  it('getHotRanking forwards custom filters', async () => {
    mockGet.mockResolvedValueOnce({
      data: { ok: true, data: { items: [], total: 0 } },
    });

    await api.getHotRanking({
      limit: 5,
      category: 'AI安全漏洞与攻击',
      date_range: '1d',
    });

    expect(mockGet).toHaveBeenCalledWith('/articles/hot', {
      params: {
        limit: 5,
        category: 'AI安全漏洞与攻击',
        date_range: '1d',
      },
    });
  });
});

// ═══════════════════════════════════════════════════════════
// Pipeline API
// ═══════════════════════════════════════════════════════════

describe('Pipeline API', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await setupApi();
  });

  it('run calls POST /pipeline/run', async () => {
    const mockResult = { pipeline_id: 'p1', status: 'completed', state: {} };
    mockPost.mockResolvedValueOnce({ data: mockResult });

    const result = await api.run(3);
    expect(mockPost).toHaveBeenCalledWith('/pipeline/run', { crawl_days: 3 });
    expect(result.status).toBe('completed');
  });

  it('crawl calls POST /pipeline/crawl', async () => {
    mockPost.mockResolvedValueOnce({ data: { pipeline_id: 'p2', status: 'completed' } });
    await api.crawl(2);
    expect(mockPost).toHaveBeenCalledWith('/pipeline/crawl', { crawl_days: 2 });
  });

  it('score calls POST /pipeline/score', async () => {
    mockPost.mockResolvedValueOnce({ data: { pipeline_id: 'p3', status: 'completed' } });
    await api.score();
    expect(mockPost).toHaveBeenCalledWith('/pipeline/score', {});
  });

  it('report calls POST /pipeline/report', async () => {
    mockPost.mockResolvedValueOnce({ data: { pipeline_id: 'p4', status: 'completed' } });
    await api.report();
    expect(mockPost).toHaveBeenCalledWith('/pipeline/report', {});
  });

  it('getStatus calls GET /pipeline/status', async () => {
    const status = { status: 'idle', current_phase: '', errors: [] };
    mockGet.mockResolvedValueOnce({ data: status });

    const result = await api.getStatus();
    expect(mockGet).toHaveBeenCalledWith('/pipeline/status');
    expect(result.status).toBe('idle');
  });

  it('getTaskStatus and getTasks call async task endpoints', async () => {
    const task = { task_id: 'task-1', status: 'running', progress: { phase: 'score' } };
    mockGet
      .mockResolvedValueOnce({ data: { data: task } })
      .mockResolvedValueOnce({ data: { data: { items: [task], total: 1 } } });

    const status = await api.getTaskStatus('task-1');
    const tasks = await api.getTasks(2, 10);

    expect(mockGet).toHaveBeenNthCalledWith(1, '/pipeline/tasks/task-1');
    expect(mockGet).toHaveBeenNthCalledWith(2, '/pipeline/tasks', {
      params: { page: 2, page_size: 10 },
    });
    expect(status.task_id).toBe('task-1');
    expect(tasks.total).toBe(1);
  });

  it('creates V2 classification and scoring background tasks', async () => {
    mockPost
      .mockResolvedValueOnce({ data: { ok: true, data: { task_id: 'classify-1', total: 12 } } })
      .mockResolvedValueOnce({ data: { ok: true, data: { task_id: 'score-1', total: 7 } } });

    const classifyTask = await api.classifyV2Task();
    const scoreTask = await api.scoreV2Task();

    expect(mockPost).toHaveBeenNthCalledWith(1, '/pipeline/classify-v2/tasks');
    expect(mockPost).toHaveBeenNthCalledWith(2, '/pipeline/score-v2/tasks');
    expect(classifyTask.data.total).toBe(12);
    expect(scoreTask.data.total).toBe(7);
  });
});

// ═══════════════════════════════════════════════════════════
// Reports API
// ═══════════════════════════════════════════════════════════

describe('Reports API', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await setupApi();
  });

  it('getReports calls GET /reports with pagination', async () => {
    const mockData = { items: [], total: 0, page: 1, page_size: 10, pages: 0 };
    mockGet.mockResolvedValueOnce({ data: mockData });

    await api.getReports(2, 10);
    expect(mockGet).toHaveBeenCalledWith('/reports', {
      params: { page: 2, page_size: 10 },
    });
  });

  it('getReport calls GET /reports/:id', async () => {
    const report = { _id: 'r1', title: 'PR Report', content_md: '# Report' } as Report;
    mockGet.mockResolvedValueOnce({ data: report });

    const result = await api.getReport('r1');
    expect(mockGet).toHaveBeenCalledWith('/reports/r1');
    expect(result.title).toBe('PR Report');
  });

  it('getKnowledge calls GET /knowledge', async () => {
    mockGet.mockResolvedValueOnce({
      data: { loaded: true, product_name: '测试', features_count: 3 },
    });

    const result = await api.getKnowledge();
    expect(mockGet).toHaveBeenCalledWith('/knowledge');
    expect(result.loaded).toBe(true);
  });
});

// ═══════════════════════════════════════════════════════════
// Feedback API
// ═══════════════════════════════════════════════════════════

describe('Feedback API', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await setupApi();
  });

  it('create calls POST /feedback and unwraps data', async () => {
    mockPost.mockResolvedValueOnce({
      data: { ok: true, data: { feedback_id: 'fb-1', created_at: '2026-07-10' } },
    });

    const { feedbackApi } = await import('./client');
    const result = await feedbackApi.create({
      target_type: 'draft',
      target_ref: { article_url_hash: 'abc', draft_index: 0 },
      rating: 5,
      comment: '很好',
      tags: ['角度好'],
    });

    expect(mockPost).toHaveBeenCalledWith('/feedback', {
      target_type: 'draft',
      target_ref: { article_url_hash: 'abc', draft_index: 0 },
      rating: 5,
      comment: '很好',
      tags: ['角度好'],
    });
    expect(result.feedback_id).toBe('fb-1');
  });

  it('list calls GET /feedback with params', async () => {
    mockGet.mockResolvedValueOnce({
      data: { ok: true, data: { items: [], total: 0, avg_rating: 0, page: 1, page_size: 20 } },
    });

    const { feedbackApi } = await import('./client');
    const result = await feedbackApi.list({ target_type: 'draft', page: 1 });

    expect(mockGet).toHaveBeenCalledWith('/feedback', {
      params: { target_type: 'draft', page: 1 },
    });
    expect(result.total).toBe(0);
  });

  it('stats calls GET /feedback/stats', async () => {
    mockGet.mockResolvedValueOnce({
      data: { ok: true, data: { groups: [], total: 0, overall_avg: 0 } },
    });

    const { feedbackApi } = await import('./client');
    await feedbackApi.stats('perspective');

    expect(mockGet).toHaveBeenCalledWith('/feedback/stats', {
      params: { group_by: 'perspective' },
    });
  });

  it('update calls PUT /feedback/:id', async () => {
    mockPut.mockResolvedValueOnce({
      data: { ok: true, data: { feedback_id: 'fb-1', updated: true, updated_at: 'now' } },
    });

    const { feedbackApi } = await import('./client');
    const result = await feedbackApi.update('fb-1', { rating: 4 });

    expect(mockPut).toHaveBeenCalledWith('/feedback/fb-1', { rating: 4 });
    expect(result.updated).toBe(true);
  });

  it('remove calls DELETE /feedback/:id', async () => {
    mockDelete.mockResolvedValueOnce({
      data: { ok: true, data: { feedback_id: 'fb-1', deleted: true } },
    });

    const { feedbackApi } = await import('./client');
    const result = await feedbackApi.remove('fb-1');

    expect(mockDelete).toHaveBeenCalledWith('/feedback/fb-1');
    expect(result.deleted).toBe(true);
  });
});

// ═══════════════════════════════════════════════════════════
// Activity API
// ═══════════════════════════════════════════════════════════

describe('Activity API', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await setupApi();
  });

  it('log calls POST /activities/log', async () => {
    mockPost.mockResolvedValueOnce({
      data: { ok: true, data: { activity_id: 'act-1', created_at: 'now' } },
    });

    const { activityApi } = await import('./client');
    const result = await activityApi.log({
      action: 'draft_download',
      target: { article_url_hash: 'abc', draft_index: 0 },
    });

    expect(mockPost).toHaveBeenCalledWith('/activities/log', {
      action: 'draft_download',
      target: { article_url_hash: 'abc', draft_index: 0 },
    });
    expect(result.activity_id).toBe('act-1');
  });

  it('batchLog calls POST /activities/batch-log', async () => {
    const activities = [
      { action: 'draft_view' as const, target: { article_url_hash: 'abc' } },
      { action: 'draft_download' as const, target: { article_url_hash: 'abc' } },
    ];
    mockPost.mockResolvedValueOnce({
      data: { ok: true, data: { activity_ids: ['a1', 'a2'], recorded: 2, failed: 0 } },
    });

    const { activityApi } = await import('./client');
    const result = await activityApi.batchLog(activities);

    expect(mockPost).toHaveBeenCalledWith('/activities/batch-log', { activities });
    expect(result.recorded).toBe(2);
  });

  it('list calls GET /activities with params', async () => {
    mockGet.mockResolvedValueOnce({
      data: { ok: true, data: { items: [], total: 0, page: 1, page_size: 20 } },
    });

    const { activityApi } = await import('./client');
    await activityApi.list({ action: 'draft_download', page: 2 });

    expect(mockGet).toHaveBeenCalledWith('/activities', {
      params: { action: 'draft_download', page: 2 },
    });
  });

  it('stats calls GET /activities/stats', async () => {
    mockGet.mockResolvedValueOnce({
      data: { ok: true, data: { total: 0, by_action: {}, by_template: {}, daily_trend: [] } },
    });

    const { activityApi } = await import('./client');
    const result = await activityApi.stats(7);

    expect(mockGet).toHaveBeenCalledWith('/activities/stats', {
      params: { days: 7 },
    });
    expect(result.total).toBe(0);
  });
});

// ═══════════════════════════════════════════════════════════
// Profile API
// ═══════════════════════════════════════════════════════════

describe('Profile API', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await setupApi();
  });

  it('getStyle calls GET /profile/style', async () => {
    mockGet.mockResolvedValueOnce({
      data: {
        ok: true,
        data: {
          user_id: 'local-user',
          style_hints: {
            preferred_templates: [],
            preferred_perspectives: [],
            preferred_length: 'medium',
            preferred_tone: 'market_oriented',
            common_revise_directions: [],
            avoid_patterns: [],
          },
          preference_scores: { template_scores: {}, perspective_scores: {} },
          feedback_summary: {
            total_feedbacks: 0,
            avg_rating: 0,
            positive_count: 0,
            negative_count: 0,
            neutral_count: 0,
            top_tags: [],
          },
          activity_summary: {
            total_downloads: 0,
            total_applies: 0,
            total_revises: 0,
            total_feedbacks: 0,
          },
          revise_instruction_patterns: [],
          llm_analysis: '',
          version: 1,
          created_at: 'now',
          updated_at: 'now',
        },
      },
    });

    const { profileApi } = await import('./client');
    const result = await profileApi.getStyle();

    expect(mockGet).toHaveBeenCalledWith('/profile/style');
    expect(result.user_id).toBe('local-user');
  });

  it('rebuild calls POST /profile/rebuild', async () => {
    mockPost.mockResolvedValueOnce({
      data: {
        ok: true,
        data: {
          rebuilt: true,
          feedback_count: 5,
          activity_count: 8,
          version: 2,
          updated_at: 'now',
        },
      },
    });

    const { profileApi } = await import('./client');
    const result = await profileApi.rebuild();

    expect(mockPost).toHaveBeenCalledWith('/profile/rebuild');
    expect(result.version).toBe(2);
  });
});

// ═══════════════════════════════════════════════════════════
// Developer Logs API
// ═══════════════════════════════════════════════════════════

describe('Developer Logs API', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await setupApi();
  });

  it('query serializes multi-select filters and unwraps the result', async () => {
    const result = {
      logs: [],
      phases: ['crawl', 'draft'],
      levels: ['INFO', 'ERROR'],
      users: [],
      total: 0,
      page: 2,
      page_size: 20,
    };
    mockGet.mockResolvedValueOnce({ data: { ok: true, data: result } });
    const { devLogsApi } = await import('./client');

    const response = await devLogsApi.query({
      date: '2026-07-13',
      phase: ['crawl', 'draft'],
      level: ['INFO', 'ERROR'],
      page: 2,
      page_size: 20,
    });

    expect(mockGet).toHaveBeenCalledWith('/dev/logs', {
      params: {
        date: '2026-07-13',
        phase: 'crawl,draft',
        level: 'INFO,ERROR',
        page: 2,
        page_size: 20,
      },
    });
    expect(response).toEqual(result);
  });

  it('calls dates, trace and stats endpoints', async () => {
    mockGet
      .mockResolvedValueOnce({
        data: { ok: true, data: { dates: ['2026-07-13'] } },
      })
      .mockResolvedValueOnce({
        data: { ok: true, data: { trace_id: 'trace/a', events: [] } },
      })
      .mockResolvedValueOnce({
        data: { ok: true, data: { total: 3, error_count: 1 } },
      });
    const { devLogsApi } = await import('./client');

    const dates = await devLogsApi.dates();
    const traceResult = await devLogsApi.trace('trace/a');
    const statsResult = await devLogsApi.stats('2026-07-13');

    expect(mockGet).toHaveBeenNthCalledWith(1, '/dev/logs/dates');
    expect(mockGet).toHaveBeenNthCalledWith(2, '/dev/logs/trace/trace%2Fa');
    expect(mockGet).toHaveBeenNthCalledWith(3, '/dev/logs/stats', {
      params: { date: '2026-07-13' },
    });
    expect(dates).toEqual(['2026-07-13']);
    expect(traceResult.trace_id).toBe('trace/a');
    expect(statsResult.error_count).toBe(1);
  });
});

describe('PR Template API', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await setupApi();
  });

  it('lists and reads tenant templates', async () => {
    mockGet
      .mockResolvedValueOnce({ data: { data: { items: [], total: 0 } } })
      .mockResolvedValueOnce({ data: { data: { template_key: 'breaking_a' } } });
    const { prTemplateApi } = await import('./client');

    await prTemplateApi.list('爆点事件');
    await prTemplateApi.get('breaking_a');

    expect(mockGet).toHaveBeenNthCalledWith(1, '/pr-templates', {
      params: { category_v2: '爆点事件' },
    });
    expect(mockGet).toHaveBeenNthCalledWith(2, '/pr-templates/breaking_a');
  });

  it('saves and previews without invoking a generation endpoint', async () => {
    const payload = {
      name: '自定义模板',
      title_template: '# 标题',
      sections: [{ heading: '概述', guide: '说明', order: 1 }],
      perspectives: ['技术视角', '市场视角'] as [string, string],
      extra_instructions: '',
      expected_version: 2,
    };
    mockPut.mockResolvedValueOnce({ data: { data: { version: 3 } } });
    mockPost.mockResolvedValueOnce({ data: { data: { content_md: '# 预览' } } });
    const { prTemplateApi } = await import('./client');

    await prTemplateApi.save('breaking_a', payload);
    const preview = await prTemplateApi.preview('breaking_a', payload);

    expect(mockPut).toHaveBeenCalledWith('/pr-templates/breaking_a', payload);
    expect(mockPost).toHaveBeenCalledWith('/pr-templates/breaking_a/preview', payload);
    expect(preview).toBe('# 预览');
  });

  it('resets, lists versions and restores a selected version', async () => {
    mockPost
      .mockResolvedValueOnce({ data: { data: { source: 'system' } } })
      .mockResolvedValueOnce({ data: { data: { version: 5 } } });
    mockGet.mockResolvedValueOnce({
      data: { data: { items: [], total: 0, page: 2, page_size: 10 } },
    });
    const { prTemplateApi } = await import('./client');

    await prTemplateApi.reset('breaking_a');
    await prTemplateApi.versions('breaking_a', 2, 10);
    await prTemplateApi.restore('breaking_a', 3);

    expect(mockPost).toHaveBeenNthCalledWith(1, '/pr-templates/breaking_a/reset');
    expect(mockGet).toHaveBeenCalledWith('/pr-templates/breaking_a/versions', {
      params: { page: 2, page_size: 10 },
    });
    expect(mockPost).toHaveBeenNthCalledWith(2, '/pr-templates/breaking_a/versions/3/restore');
  });
});

describe('User Prompt API', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await setupApi();
  });

  it('gets the effective draft prompt', async () => {
    mockGet.mockResolvedValueOnce({
      data: { data: { prompt_key: 'draft_system', content: 'default', is_custom: false } },
    });
    const { promptApi } = await import('./client');

    const result = await promptApi.getDraftPrompt();

    expect(mockGet).toHaveBeenCalledWith('/user-prompts/draft-system');
    expect(result.is_custom).toBe(false);
  });

  it('saves a custom draft prompt', async () => {
    mockPut.mockResolvedValueOnce({
      data: { data: { prompt_key: 'draft_system', content: 'custom', is_custom: true } },
    });
    const { promptApi } = await import('./client');

    const result = await promptApi.saveDraftPrompt('custom');

    expect(mockPut).toHaveBeenCalledWith('/user-prompts/draft-system', { content: 'custom' });
    expect(result.is_custom).toBe(true);
  });

  it('resets the custom draft prompt', async () => {
    mockPost.mockResolvedValueOnce({
      data: { data: { prompt_key: 'draft_system', content: 'default', is_custom: false } },
    });
    const { promptApi } = await import('./client');

    const result = await promptApi.resetDraftPrompt();

    expect(mockPost).toHaveBeenCalledWith('/user-prompts/draft-system/reset');
    expect(result.is_custom).toBe(false);
  });
});
