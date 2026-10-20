import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api, {
  exportOpmlUrl: vi.fn(() => '/istota/api/feeds/opml'),
});

import { getFeedsConfig, getModuleServices } from '$lib/api';
import Page from './+page.svelte';

afterEach(cleanup);

// A failed first load used to sit in a banner over an empty, editable config,
// whose one save would have sent that empty draft. It takes the pane instead.
it('gives a failed first load the whole pane, with a Retry that recovers', async () => {
  vi.mocked(getModuleServices).mockResolvedValue({ module_enabled: true, services: [] } as never);
  vi.mocked(getFeedsConfig)
    .mockRejectedValueOnce(new Error('Feeds database unavailable'))
    .mockResolvedValue({
      config: { settings: {}, categories: [{ slug: 'blogs', title: 'Blogs' }], feeds: [] },
      diagnostics: null,
      feed_state: [],
    } as never);

  const { container } = render(Page);
  const failure = await waitFor(() => {
    const el = container.querySelector('.center-msg.error');
    expect(el).not.toBeNull();
    return el as HTMLElement;
  });
  expect(failure.textContent).toContain('Feeds database unavailable');
  expect(screen.queryByRole('button', { name: '+ Add category' })).toBeNull();

  await fireEvent.click(within(failure).getByRole('button', { name: 'Retry' }));
  await screen.findByRole('button', { name: '+ Add category' });
  expect(container.querySelector('.center-msg.error')).toBeNull();
});
