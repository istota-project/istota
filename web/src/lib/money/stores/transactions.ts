import { writable } from 'svelte/store';
import { getContext, setContext } from 'svelte';
import { base } from '$app/paths';

/** Full account name selected in the sidebar, or empty string for all. */
export const selectedAccount = writable('');

/** Year filter, or 0 for all years. */
export const selectedYear = writable(new Date().getFullYear());

/** Free-text filter for payee/narration, or #tag for tag filter. */
export const filterText = writable('');

export type TransactionSelection = { account: string; year: number };

export function encodeTransactionSelection(
  selection: TransactionSelection,
): Record<string, string> {
  const params: Record<string, string> = {};
  if (selection.account) params.account = selection.account;
  params.year = selection.year ? String(selection.year) : 'all';
  return params;
}

export function transactionsUrl(account: string, year: number): string {
  const query = new URLSearchParams(encodeTransactionSelection({ account, year }));
  return `${base}/money/transactions/?${query}`;
}

const TRANSACTION_NAVIGATION = Symbol('transaction-navigation');
type TransactionNavigation = (account: string) => void;

export function setTransactionNavigation(navigate: TransactionNavigation): void {
  setContext(TRANSACTION_NAVIGATION, navigate);
}

export function getTransactionNavigation(): TransactionNavigation {
  const navigate = getContext<TransactionNavigation | undefined>(TRANSACTION_NAVIGATION);
  if (!navigate) throw new Error('getTransactionNavigation() outside the transactions layout');
  return navigate;
}
