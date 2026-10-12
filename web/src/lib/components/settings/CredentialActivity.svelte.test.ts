import { afterEach, expect, it, vi } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { cleanup, render, screen } from '@testing-library/svelte';
import CredentialActivity from './CredentialActivity.svelte';
vi.mock('$app/paths', () => ({ base: '/istota' }));
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
import { getCredentialActivity } from '$lib/api';
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
it('shows credential activity with actor and date', async () => {
  vi.mocked(getCredentialActivity).mockResolvedValue([
    {
      id: 1,
      action: 'reveal',
      name: 'example',
      actor: 'web:alice',
      at: '2026-01-01',
      detail: null,
    },
  ]);
  render(CredentialActivity);
  expect(await screen.findByText('reveal · example')).toBeTruthy();
  expect(screen.getByText(/web:alice/)).toBeTruthy();
});
