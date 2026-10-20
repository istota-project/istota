import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';

const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);

// A failed CSV import is an action on a page still on screen: a notice, not the
// pane's load failure, which used to replace the labs page with the message.
const notices = vi.hoisted(() => ({ notifyError: vi.fn() }));
vi.mock('$lib/stores/notices', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/stores/notices')>()),
  notifyError: notices.notifyError,
}));

await fillApiDouble(api, {
  getLabsMatrix: vi.fn(async () => ({ categories: [], panels: [], values: {} })),
  listHealthPanels: vi.fn(async () => ({ panels: [] })),
  importHealthCsv: vi.fn(async () => {
    throw new Error('Row 3 has no date');
  }),
});

import Page from './+page.svelte';

afterEach(() => {
  cleanup();
  notices.notifyError.mockClear();
});

it('reports a failed CSV import as a notice and keeps the page', async () => {
  const { container } = render(Page);
  await screen.findByText('No lab results on file yet.');
  const input = container.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File(['date,name\n'], 'labs.csv', { type: 'text/csv' });
  Object.defineProperty(input, 'files', { value: [file] });
  await fireEvent.change(input);

  await waitFor(() => expect(notices.notifyError).toHaveBeenCalledWith('Row 3 has no date'));
  expect(container.querySelector('.center-msg.error')).toBeNull();
  expect(screen.getByText('No lab results on file yet.')).toBeTruthy();
  expect(screen.getByRole('heading', { name: 'Labs' })).toBeTruthy();
});
