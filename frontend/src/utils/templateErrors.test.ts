import { describe, expect, it } from 'vitest';
import { templateErrorMessage } from './templateErrors';

describe('templateErrorMessage', () => {
  it('explains optimistic concurrency conflicts', () => {
    expect(templateErrorMessage({ response: { status: 409 } }, 'fallback')).toContain('刷新');
  });

  it('explains expired authentication', () => {
    expect(templateErrorMessage({ response: { status: 401 } }, 'fallback')).toContain('重新登录');
  });

  it('formats backend field validation details', () => {
    const message = templateErrorMessage(
      {
        response: {
          status: 422,
          data: {
            detail: [{ loc: ['body', 'sections', 0, 'heading'], msg: 'Field required' }],
          },
        },
      },
      'fallback',
    );

    expect(message).toBe('sections.0.heading: Field required');
  });
});
