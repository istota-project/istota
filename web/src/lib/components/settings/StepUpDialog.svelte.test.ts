import { afterEach, expect, it, vi } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import StepUpDialog from './StepUpDialog.svelte';
vi.mock('$app/paths', () => ({ base: '/istota' }));
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
import { startStepUp } from '$lib/api';
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
it('keeps a bad code open and starts again when the request is dead', async () => {
  vi.mocked(startStepUp).mockResolvedValue({
    request_id: 'request-1',
    method: 'email' as const,
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

it('asks for the password, says why, and claims no code was sent', async () => {
  vi.mocked(startStepUp).mockResolvedValue({
    method: 'password',
    request_id: 'request-2',
    expires_at: 'later',
  });
  const submit = vi
    .fn()
    .mockRejectedValueOnce(
      Object.assign(new Error('That password was not accepted. Try again.'), { reason: 'bad' }),
    )
    .mockResolvedValue(undefined);
  const done = vi.fn();
  render(StepUpDialog, {
    action: 'admin_config_change',
    onConfirm: submit,
    onComplete: done,
    onCancel: vi.fn(),
  });
  await screen.findByText(/Mail isn't set up on this install, so confirm with your password/);
  expect(screen.queryByText(/code/i)).toBeNull();
  expect(screen.queryByRole('button', { name: 'Send new code' })).toBeNull();
  const field = screen.getByLabelText('Account password') as HTMLInputElement;
  expect(field.type).toBe('password');
  expect(field.autocomplete).toBe('current-password');
  await fireEvent.input(field, { target: { value: 'not it' } });
  await fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  await screen.findByText('That password was not accepted. Try again.');
  expect(startStepUp).toHaveBeenCalledTimes(1);
  expect(field.value).toBe('');
  await fireEvent.input(field, { target: { value: 'the passphrase' } });
  await fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  await waitFor(() => expect(done).toHaveBeenCalled());
  expect(submit).toHaveBeenLastCalledWith({ request_id: 'request-2', password: 'the passphrase' });
});
