import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import StepUpDialog from './StepUpDialog.svelte';
vi.mock('$app/paths', () => ({ base: '/istota' }));
vi.mock('$lib/api', async (original) => ({
  ...(await original<typeof import('$lib/api')>()),
  startStepUp: vi.fn(),
}));
import { startStepUp } from '$lib/api';
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
it('keeps a bad code open and starts again when the request is dead', async () => {
  vi.mocked(startStepUp).mockResolvedValue({
    request_id: 'request-1',
    email_hint: 'a•••@example.com',
    expires_at: 'later',
  });
  const submit = vi
    .fn()
    .mockRejectedValueOnce(Object.assign(new Error('Try again'), { reason: 'bad' }))
    .mockRejectedValueOnce(Object.assign(new Error('Expired'), { reason: 'dead' }))
    .mockResolvedValue(undefined);
  const done = vi.fn();
  render(StepUpDialog, {
    action: 'export',
    onConfirm: submit,
    onComplete: done,
    onCancel: vi.fn(),
  });
  await screen.findByText(/a•••@example.com/);
  await fireEvent.input(screen.getByLabelText('Confirmation code'), {
    target: { value: '123456' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  await screen.findByText('Try again');
  expect(startStepUp).toHaveBeenCalledTimes(1);
  await fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  await waitFor(() => expect(startStepUp).toHaveBeenCalledTimes(2));
  expect((screen.getByLabelText('Confirmation code') as HTMLInputElement).value).toBe('');
  await fireEvent.input(screen.getByLabelText('Confirmation code'), {
    target: { value: '654321' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  await waitFor(() => expect(done).toHaveBeenCalled());
  expect(submit).toHaveBeenLastCalledWith({ request_id: 'request-1', code: '654321' });
});
