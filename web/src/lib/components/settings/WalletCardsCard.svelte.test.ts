import { afterEach, expect, it, vi } from 'vitest';
import { fillApiDouble, type ApiDouble } from '$lib/test/apiDouble';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import WalletCardsCard from './WalletCardsCard.svelte';
vi.mock('$app/paths', () => ({ base: '/istota' }));
const api = vi.hoisted(() => ({}) as ApiDouble);
vi.mock('$lib/api', () => api);
await fillApiDouble(api);
import { AuthError, removeWalletCard, type WalletCard } from '$lib/api';

const card: WalletCard = {
  id: 7,
  label: 'Groceries',
  brand: 'Visa',
  last_four: '4242',
  exp_month: 1,
  exp_year: 2099,
  name: 'Card Holder',
  billing: {} as WalletCard['billing'],
  state: 'active',
  issuer: 'Example Bank',
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function confirmRemove() {
  await fireEvent.click(screen.getByRole('button', { name: 'Actions for Groceries' }));
  await fireEvent.click(await screen.findByRole('menuitem', { name: 'Remove' }));
  const dialog = await screen.findByRole('dialog');
  await fireEvent.click(
    Array.from(dialog.querySelectorAll('button')).find((b) => b.textContent?.trim() === 'Remove')!,
  );
}

it('hands an expired session on removal to onError rather than the dialog', async () => {
  vi.mocked(removeWalletCard).mockRejectedValue(new AuthError());
  const onError = vi.fn();
  render(WalletCardsCard, { cards: [card], writable: true, onSaved: vi.fn(), onError });
  await confirmRemove();
  await waitFor(() => expect(onError).toHaveBeenCalledOnce());
  expect(onError.mock.calls[0][0]).toBeInstanceOf(AuthError);
});

it('shows any other refusal in the dialog', async () => {
  vi.mocked(removeWalletCard).mockRejectedValue(new Error('Card has an open purchase.'));
  const onError = vi.fn();
  render(WalletCardsCard, { cards: [card], writable: true, onSaved: vi.fn(), onError });
  await confirmRemove();
  expect(await screen.findByRole('alert')).toHaveTextContent('Card has an open purchase.');
  expect(onError).not.toHaveBeenCalled();
});
