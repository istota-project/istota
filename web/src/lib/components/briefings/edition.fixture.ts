import type { BriefingArchiveItem } from '$lib/api';

export const edition: BriefingArchiveItem = {
  id: 3,
  briefing_name: 'morning',
  subject: 'Morning edition',
  generated_at: '2026-10-15T07:00:00Z',
  task_id: null,
  delivered_to: ['email'],
  body_md: 'Headlines\n\nThe linear edition fallback.',
  document: {
    version: 1,
    title: 'Morning edition',
    blocks: [
      {
        block_id: 42,
        title: 'Headlines <em>today</em>',
        kind: 'markdown',
        body_md:
          '[A story](https://example.com/story)\n\nSecond paragraph\n\nThird paragraph\n\n[Full story](https://example.com/full)',
        presentation: { width: 'wide' },
        source_status: 'partial',
      },
      {
        block_id: 43,
        title: 'Calendar',
        kind: 'future-agenda',
        body_md: '- An appointment\n- A reminder',
        presentation: { width: 'auto' },
        source_status: 'complete',
      },
      {
        block_id: 44,
        title: 'Notes',
        kind: 'markdown',
        body_md: 'A note for today.',
        presentation: { width: 'full' },
        source_status: 'complete',
      },
    ],
  },
};
