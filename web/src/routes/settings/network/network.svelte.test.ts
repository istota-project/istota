import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, waitFor } from '@testing-library/svelte';
const navigation = vi.hoisted(() => ({ goto: vi.fn(async () => {}) }));
vi.mock('$app/navigation', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  goto: navigation.goto,
}));
vi.mock('$app/paths', () => ({ base: '/istota' }));
import Network from './+page.svelte';

afterEach(cleanup);

it('sends stored bell rows and bookmarks to Scheduled jobs, over the old entry', async () => {
  render(Network);
  await waitFor(() =>
    expect(navigation.goto).toHaveBeenCalledWith('/istota/settings/jobs', { replaceState: true }),
  );
});
