import { afterEach, expect, it, vi } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import StepUpDialog from './StepUpDialog.svelte';
import { pressEnter } from '$lib/test/implicitSubmit';
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

it('offers a retry when starting the confirmation fails', async () => {
  vi.mocked(startStepUp)
    .mockRejectedValueOnce(new Error('Istota could not be reached.'))
    .mockResolvedValue({ method: 'password', request_id: 'request-3', expires_at: 'later' });
  render(StepUpDialog, {
    action: 'export',
    onConfirm: vi.fn(),
    onCancel: vi.fn(),
  });
  await screen.findByText('Istota could not be reached.');
  await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
  expect(await screen.findByLabelText('Account password')).toBeInTheDocument();
  expect(startStepUp).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
  expect(screen.queryByText('Istota could not be reached.')).toBeNull();
});

it('offers the password first and mails a code only when asked', async () => {
  vi.mocked(startStepUp)
    .mockResolvedValueOnce({
      method: 'password',
      methods: ['password', 'email'],
      request_id: 'request-4',
      expires_at: 'later',
      email_hint: 'a•••@example.com',
    })
    .mockResolvedValueOnce({
      method: 'email',
      methods: ['password', 'email'],
      request_id: 'request-5',
      expires_at: 'later',
      email_hint: 'a•••@example.com',
    });
  const submit = vi.fn().mockResolvedValue(undefined);
  render(StepUpDialog, { action: 'admin_config_change', onConfirm: submit, onCancel: vi.fn() });
  await screen.findByText('Confirm with your account password.');
  expect(screen.queryByText(/Mail isn't set up/)).toBeNull();
  expect(startStepUp).toHaveBeenCalledWith('admin_config_change', undefined);
  await fireEvent.click(screen.getByRole('button', { name: 'Email me a code instead' }));
  await screen.findByLabelText('Confirmation code');
  expect(startStepUp).toHaveBeenLastCalledWith('admin_config_change', undefined, 'email');
  await screen.findByText(/a•••@example.com/);
  await fireEvent.input(screen.getByLabelText('Confirmation code'), {
    target: { value: '123456' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  await waitFor(() =>
    expect(submit).toHaveBeenLastCalledWith({ request_id: 'request-5', code: '123456' }),
  );
});

it('goes back to the password from a mailed code', async () => {
  vi.mocked(startStepUp)
    .mockResolvedValueOnce({
      method: 'email',
      methods: ['password', 'email'],
      request_id: 'request-6',
      expires_at: 'later',
      email_hint: 'a•••@example.com',
    })
    .mockResolvedValueOnce({
      method: 'password',
      methods: ['password', 'email'],
      request_id: 'request-7',
      expires_at: 'later',
      email_hint: 'a•••@example.com',
    });
  render(StepUpDialog, { action: 'export', onConfirm: vi.fn(), onCancel: vi.fn() });
  await screen.findByLabelText('Confirmation code');
  await fireEvent.click(screen.getByRole('button', { name: 'Use my password instead' }));
  expect(await screen.findByLabelText('Account password')).toBeInTheDocument();
  expect(startStepUp).toHaveBeenLastCalledWith('export', undefined, 'password');
});

it('says when codes are used up and keeps the password usable', async () => {
  vi.mocked(startStepUp)
    .mockResolvedValueOnce({
      method: 'password',
      methods: ['password', 'email'],
      request_id: 'request-8',
      expires_at: 'later',
      email_hint: 'a•••@example.com',
    })
    .mockRejectedValueOnce(
      Object.assign(new Error('Too many confirmation codes requested. Try again in 12 minutes.'), {
        reason: 'mail_budget',
      }),
    );
  const submit = vi.fn().mockResolvedValue(undefined);
  render(StepUpDialog, { action: 'admin_config_change', onConfirm: submit, onCancel: vi.fn() });
  await screen.findByText('Confirm with your account password.');
  await fireEvent.click(screen.getByRole('button', { name: 'Email me a code instead' }));
  await screen.findByText('Too many confirmation codes requested. Try again in 12 minutes.');
  const field = screen.getByLabelText('Account password') as HTMLInputElement;
  await fireEvent.input(field, { target: { value: 'the passphrase' } });
  await fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  await waitFor(() =>
    expect(submit).toHaveBeenLastCalledWith({
      request_id: 'request-8',
      password: 'the passphrase',
    }),
  );
});

it('drops an expired code when no new one can be sent', async () => {
  vi.mocked(startStepUp)
    .mockResolvedValueOnce({
      method: 'email',
      methods: ['email'],
      request_id: 'request-9',
      expires_at: 'later',
      email_hint: 'a•••@example.com',
    })
    .mockRejectedValueOnce(
      Object.assign(new Error('Too many confirmation codes requested. Try again in 5 minutes.'), {
        reason: 'mail_budget',
      }),
    );
  const submit = vi
    .fn()
    .mockRejectedValue(Object.assign(new Error('That code has expired.'), { reason: 'dead' }));
  render(StepUpDialog, { action: 'export', onConfirm: submit, onCancel: vi.fn() });
  await fireEvent.input(await screen.findByLabelText('Confirmation code'), {
    target: { value: '123456' },
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  await screen.findByText('Too many confirmation codes requested. Try again in 5 minutes.');
  expect(screen.getByRole('button', { name: 'Confirm' })).toBeDisabled();
  expect(submit).toHaveBeenCalledTimes(1);
});

it('confirms on Enter in the code field', async () => {
  vi.mocked(startStepUp).mockResolvedValue({
    request_id: 'request-3',
    method: 'email' as const,
    email_hint: 'a•••@example.com',
    expires_at: 'later',
  });
  const submit = vi.fn().mockResolvedValue(undefined);
  render(StepUpDialog, { action: 'export', onConfirm: submit, onCancel: vi.fn() });
  const code = await screen.findByLabelText('Confirmation code');
  await fireEvent.input(code, { target: { value: '123456' } });
  await pressEnter(code);
  await waitFor(() =>
    expect(submit).toHaveBeenCalledWith({ request_id: 'request-3', code: '123456' }),
  );
});
