import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import api from '../api/client';
import DraftFeedback from './DraftFeedback';

vi.mock('../api/client', () => ({
  default: {
    create: vi.fn(),
  },
}));

const defaultProps = {
  articleUrlHash: 'abc123def45678901234567890123456',
  draftIndex: 1,
  template: '爆点A',
  perspective: '产品能力视角',
};

function clickRate(starIndex: number) {
  const stars = screen.getAllByRole('radio');
  fireEvent.click(stars[starIndex]);
}

describe('DraftFeedback', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.create).mockResolvedValue({
      feedback_id: 'feedback-1',
      created_at: '2026-07-10T10:00:00Z',
    });
  });

  it('renders rating, tags and text input', () => {
    render(<DraftFeedback {...defaultProps} initialRating={4} />);

    expect(screen.getByText('草稿反馈')).toBeDefined();
    expect(screen.getByText('历史评分：4 星')).toBeDefined();
    expect(screen.getByText('结构清晰')).toBeDefined();
    expect(screen.getByPlaceholderText(/补充你的具体意见/)).toBeDefined();
    expect(screen.getByText('爆点A / 产品能力视角')).toBeDefined();
  });

  it('submits draft feedback with rating, comment and selected tag', async () => {
    const onSubmitted = vi.fn();
    render(<DraftFeedback {...defaultProps} onSubmitted={onSubmitted} />);

    clickRate(4);
    fireEvent.click(screen.getByText('结构清晰'));
    fireEvent.change(screen.getByPlaceholderText(/补充你的具体意见/), {
      target: { value: '这版可以直接使用' },
    });
    fireEvent.click(screen.getByTestId('draft-feedback-submit'));

    await waitFor(() => {
      expect(api.create).toHaveBeenCalledWith({
        target_type: 'draft',
        target_ref: {
          article_url_hash: defaultProps.articleUrlHash,
          draft_index: 1,
          revision_id: undefined,
        },
        rating: 5,
        comment: '这版可以直接使用',
        tags: ['结构清晰'],
      });
    });
    expect(onSubmitted).toHaveBeenCalledWith('feedback-1');
    expect(screen.getByText('已提交')).toBeDefined();
  });

  it('submits revision feedback when revisionId is provided', async () => {
    render(<DraftFeedback {...defaultProps} revisionId="rev-1" />);

    clickRate(2);
    fireEvent.click(screen.getByTestId('draft-feedback-submit'));

    await waitFor(() => {
      expect(api.create).toHaveBeenCalledWith(
        expect.objectContaining({
          target_type: 'revision',
          target_ref: expect.objectContaining({
            revision_id: 'rev-1',
          }),
          rating: 3,
        }),
      );
    });
  });

  it('does not submit without a rating', () => {
    render(<DraftFeedback {...defaultProps} />);

    fireEvent.click(screen.getByTestId('draft-feedback-submit'));

    expect(api.create).not.toHaveBeenCalled();
  });
});
