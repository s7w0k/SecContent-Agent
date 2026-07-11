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

  it('getArticle calls GET /articles/:hash', async () => {
    const article = { _id: '1', title: 'Test', url_hash: 'abc' } as Article;
    mockGet.mockResolvedValueOnce({ data: article });

    const result = await api.getArticle('abc');
    expect(mockGet).toHaveBeenCalledWith('/articles/abc');
    expect(result).toEqual(article);
  });

  it('getStats returns stats data', async () => {
    const stats = { total_articles: 42, ai_security_count: 10, high_value_count: 5 };
    mockGet.mockResolvedValueOnce({ data: stats });

    const result = await api.getStats();
    expect(mockGet).toHaveBeenCalledWith('/stats');
    expect(result.total_articles).toBe(42);
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
// Chat API
// ═══════════════════════════════════════════════════════════

describe('Chat API', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await setupApi();
  });

  it('ask calls POST /chat/ask', async () => {
    const mockResponse = {
      ok: true,
      data: { answer: '测试回答', references: ['knowledge'] },
    };
    mockPost.mockResolvedValueOnce({ data: mockResponse });

    const { chatApi } = await import('./client');
    const result = await chatApi.ask({ message: '问题' });

    expect(mockPost).toHaveBeenCalledWith('/chat/ask', { message: '问题' });
    expect(result.answer).toBe('测试回答');
    expect(result.references).toEqual(['knowledge']);
  });

  it('askStream calls POST /chat/ask_stream and processes SSE events', async () => {
    // Mock fetch for SSE streaming
    const encoder = new TextEncoder();
    const sseData = [
      encoder.encode('data: {"chunk":"你好"}\n\n'),
      encoder.encode('data: {"chunk":"世界"}\n\n'),
      encoder.encode('data: {"done":true,"answer":"你好世界"}\n\n'),
    ];

    const mockReader = {
      read: vi.fn(),
    };
    let readIndex = 0;
    mockReader.read.mockImplementation(() => {
      if (readIndex < sseData.length) {
        return Promise.resolve({ done: false, value: sseData[readIndex++] });
      }
      return Promise.resolve({ done: true, value: undefined });
    });

    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      body: { getReader: () => mockReader },
    }) as unknown as typeof fetch;

    const { chatApi } = await import('./client');
    const chunks: string[] = [];
    let doneAnswer = '';

    await chatApi.askStream(
      { message: '问题' },
      (chunk) => chunks.push(chunk),
      (answer) => {
        doneAnswer = answer;
      },
    );

    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/chat/ask_stream'),
      expect.objectContaining({ method: 'POST' }),
    );
    expect(chunks).toEqual(['你好', '世界']);
    expect(doneAnswer).toBe('你好世界');
  });

  it('reviseDraft calls POST /articles/:hash/drafts/:index/revise', async () => {
    const mockResponse = {
      ok: true,
      data: {
        revision_id: 'rev-001',
        revised_content_md: '# 新稿',
        change_summary: ['修改1'],
        saved: true,
      },
    };
    mockPost.mockResolvedValueOnce({ data: mockResponse });

    const { chatApi } = await import('./client');
    const result = await chatApi.reviseDraft('abc123', 0, {
      instruction: '改意见',
      save: true,
    });

    expect(mockPost).toHaveBeenCalledWith('/articles/abc123/drafts/0/revise', {
      instruction: '改意见',
      save: true,
    });
    expect(result.revision_id).toBe('rev-001');
    expect(result.saved).toBe(true);
  });

  it('applyRevision calls POST /articles/:hash/drafts/:index/revisions/:id/apply', async () => {
    const mockResponse = {
      ok: true,
      data: {
        article_url_hash: 'abc123',
        draft_index: 0,
        revision_id: 'rev-001',
        applied: true,
      },
    };
    mockPost.mockResolvedValueOnce({ data: mockResponse });

    const { chatApi } = await import('./client');
    const result = await chatApi.applyRevision('abc123', 0, 'rev-001');

    expect(mockPost).toHaveBeenCalledWith('/articles/abc123/drafts/0/revisions/rev-001/apply');
    expect(result.applied).toBe(true);
  });

  it('getChatHistory calls GET /articles/:hash/drafts/:index/chat-history', async () => {
    const mockResponse = {
      ok: true,
      data: {
        messages: [
          { role: 'user', content: '问题', created_at: '2026-07-07T10:00:00' },
          { role: 'assistant', content: '回答', created_at: '2026-07-07T10:00:01' },
        ],
      },
    };
    mockGet.mockResolvedValueOnce({ data: mockResponse });

    const { chatApi } = await import('./client');
    const result = await chatApi.getChatHistory('abc123', 0);

    expect(mockGet).toHaveBeenCalledWith('/articles/abc123/drafts/0/chat-history');
    expect(result).toHaveLength(2);
    expect(result[0].role).toBe('user');
    expect(result[1].role).toBe('assistant');
  });

  it('clearChatHistory calls DELETE /articles/:hash/drafts/:index/chat-history', async () => {
    const mockResponse = {
      ok: true,
      data: { cleared: true },
    };
    mockDelete.mockResolvedValueOnce({ data: mockResponse });

    const { chatApi } = await import('./client');
    const result = await chatApi.clearChatHistory('abc123', 0);

    expect(mockDelete).toHaveBeenCalledWith('/articles/abc123/drafts/0/chat-history');
    expect(result.cleared).toBe(true);
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
