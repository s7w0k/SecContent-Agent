import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { activityApi, profileApi } from '../api/client';
import type { ActivityStats, StyleProfile, UserActivity } from '../types';
import ProfilePage from './ProfilePage';

vi.mock('../api/client', () => ({
  profileApi: {
    getStyle: vi.fn(),
    rebuild: vi.fn(),
  },
  activityApi: {
    list: vi.fn(),
    stats: vi.fn(),
  },
}));

const mockProfile: StyleProfile = {
  user_id: 'local-user',
  style_hints: {
    preferred_templates: ['爆点A'],
    preferred_perspectives: ['产品能力视角'],
    preferred_length: 'medium',
    preferred_tone: 'market_oriented',
    common_revise_directions: ['减少技术细节'],
    avoid_patterns: ['标题太平'],
  },
  preference_scores: {
    template_scores: {
      爆点A: {
        count: 4,
        avg_rating: 4.5,
        download_count: 3,
        apply_count: 2,
        revise_count: 1,
      },
    },
    perspective_scores: {
      产品能力视角: {
        count: 3,
        avg_rating: 4.2,
        download_count: 2,
        apply_count: 1,
        revise_count: 2,
      },
    },
  },
  feedback_summary: {
    total_feedbacks: 8,
    avg_rating: 4.1,
    positive_count: 5,
    negative_count: 1,
    neutral_count: 2,
    top_tags: ['结构清晰', '标题有冲击力'],
  },
  activity_summary: {
    total_downloads: 6,
    total_applies: 3,
    total_revises: 4,
    total_feedbacks: 8,
    last_active_at: '2026-07-10T10:00:00Z',
  },
  revise_instruction_patterns: [{ pattern: '减少技术细节', count: 3 }],
  llm_analysis: '偏好市场传播视角，关注标题冲击力。',
  version: 2,
  created_at: '2026-07-10T09:00:00Z',
  updated_at: '2026-07-10T10:00:00Z',
};

const mockActivity: UserActivity = {
  activity_id: 'activity-1',
  user_id: 'local-user',
  action: 'draft_download',
  target: {
    article_url_hash: 'abc123def45678901234567890123456',
    draft_index: 1,
    template: '爆点A',
    perspective: '产品能力视角',
  },
  context: {
    article_title: '关键漏洞事件',
  },
  metadata: {},
  created_at: '2026-07-10T10:00:00Z',
};

const mockStats: ActivityStats = {
  total: 4,
  by_action: {
    draft_download: 2,
    revision_apply: 1,
    draft_revise: 1,
  },
  by_template: {
    爆点A: 2,
  },
  daily_trend: [
    { date: '2026-07-09', count: 1 },
    { date: '2026-07-10', count: 3 },
  ],
};

function mockActivities() {
  vi.mocked(activityApi.list).mockResolvedValue({
    items: [mockActivity],
    total: 1,
    page: 1,
    page_size: 20,
  });
  vi.mocked(activityApi.stats).mockResolvedValue(mockStats);
}

describe('ProfilePage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockActivities();
    vi.mocked(profileApi.getStyle).mockResolvedValue(mockProfile);
    vi.mocked(profileApi.rebuild).mockResolvedValue({
      rebuilt: true,
      feedback_count: 8,
      activity_count: 4,
      version: 3,
      updated_at: '2026-07-10T11:00:00Z',
    });
  });

  it('renders loading state before requests resolve', () => {
    vi.mocked(profileApi.getStyle).mockReturnValue(new Promise(() => {}));
    vi.mocked(activityApi.list).mockReturnValue(new Promise(() => {}));
    vi.mocked(activityApi.stats).mockReturnValue(new Promise(() => {}));

    render(<ProfilePage />);

    expect(screen.getByTestId('profile-loading')).toBeDefined();
  });

  it('renders profile, preferences and activity timeline', async () => {
    render(<ProfilePage />);

    await waitFor(() => {
      expect(screen.getByText('风格画像')).toBeDefined();
    });

    expect(screen.getAllByText('爆点A').length).toBeGreaterThan(0);
    expect(screen.getAllByText('产品能力视角').length).toBeGreaterThan(0);
    expect(screen.getByText('市场传播向')).toBeDefined();
    expect(screen.getByText('结构清晰')).toBeDefined();
    expect(screen.getByText('操作记录时间线')).toBeDefined();
    expect(screen.getByText('关键漏洞事件')).toBeDefined();
  });

  it('renders empty guidance when profile is not found', async () => {
    vi.mocked(profileApi.getStyle).mockRejectedValue({
      response: { status: 404, data: { detail: 'Profile not found' } },
    });

    render(<ProfilePage />);

    await waitFor(() => {
      expect(screen.getByText('用户画像尚未生成')).toBeDefined();
    });
    expect(screen.getByText(/系统会积累偏好信号/)).toBeDefined();
  });

  it('calls rebuild and reloads profile', async () => {
    render(<ProfilePage />);

    await waitFor(() => {
      expect(screen.getByText('风格画像')).toBeDefined();
    });

    fireEvent.click(screen.getByRole('button', { name: /重建画像/ }));

    await waitFor(() => {
      expect(profileApi.rebuild).toHaveBeenCalledTimes(1);
    });
    expect(profileApi.getStyle).toHaveBeenCalledTimes(2);
  });

  it('renders error alert when profile loading fails', async () => {
    vi.mocked(profileApi.getStyle).mockRejectedValue({
      response: { status: 500, data: { detail: 'Database not available' } },
    });

    render(<ProfilePage />);

    await waitFor(() => {
      expect(screen.getByText('用户画像加载异常')).toBeDefined();
    });
    expect(screen.getByText('Database not available')).toBeDefined();
  });
});
