<script lang="ts">
  import { untrack } from 'svelte';
  import type { TransactionRow } from '$lib/money/api';
  import { Modal, Button, Select, type SelectOption } from '$lib/components/ui';

  interface Props {
    txn: TransactionRow;
    /** Known account names, for the account dropdown. */
    accounts?: string[];
    onSave: (data: {
      payee: string;
      narration: string;
      date: string;
      account: string;
      position: string;
    }) => void;
    onCancel: () => void;
    error?: string;
    saving?: boolean;
  }

  let { txn, accounts = [], onSave, onCancel, error = '', saving = false }: Props = $props();

  let payeeInput: HTMLInputElement | undefined = $state();
  $effect(() => {
    payeeInput?.focus();
  });

  let payee = $state(untrack(() => txn.payee ?? ''));
  let narration = $state(untrack(() => txn.narration ?? ''));
  let date = $state(untrack(() => txn.date ?? ''));
  let account = $state(untrack(() => txn.account ?? ''));
  let position = $state(untrack(() => txn.position ?? ''));
  let open = $state(true);

  const accountOptions = $derived.by<SelectOption[]>(() => {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const a of accounts) {
      if (a && !seen.has(a)) {
        seen.add(a);
        out.push(a);
      }
    }
    if (account && !seen.has(account)) {
      seen.add(account);
      out.push(account);
    }
    return out.sort((a, b) => a.localeCompare(b)).map((a) => ({ value: a, label: a }));
  });

  function handleSave() {
    if (!account.trim()) return;
    onSave({
      payee: payee.trim(),
      narration: narration.trim(),
      date: date.trim(),
      account: account.trim(),
      position: position.trim(),
    });
  }

  function handleOpenChange(next: boolean) {
    if (!next) onCancel();
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') handleSave();
  }
</script>

<svelte:window on:keydown={handleKeydown} />

<Modal bind:open title="Edit transaction" onOpenChange={handleOpenChange} width="380px">
  <label class="field">
    <span>Payee</span>
    <input type="text" bind:this={payeeInput} bind:value={payee} placeholder="e.g. Acme Corp" />
  </label>

  <label class="field">
    <span>Narration</span>
    <input type="text" bind:value={narration} placeholder="Description" />
  </label>

  <label class="field">
    <span>Date</span>
    <input type="date" bind:value={date} />
  </label>

  <label class="field">
    <span>Account</span>
    <Select bind:value={account} options={accountOptions} fullWidth ariaLabel="Account" />
  </label>

  <label class="field">
    <span>Amount</span>
    <input type="text" bind:value={position} placeholder="e.g. -12.50 USD" />
  </label>

  {#if error}
    <div class="form-error">{error}</div>
  {/if}

  {#snippet footer()}
    <Button variant="ghost" onclick={onCancel}>Cancel</Button>
    <Button variant="primary" onclick={handleSave} disabled={!account.trim() || saving}>
      {saving ? 'Saving…' : 'Save'}
    </Button>
  {/snippet}
</Modal>

<style>
  .field {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    margin-bottom: 0.75rem;
  }

  .field span {
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  .field input[type='text'],
  .field input[type='date'] {
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-sm);
    padding: 0.35rem 0.5rem;
    border-radius: 0.25rem;
  }

  .form-error {
    font-size: var(--text-xs);
    color: #d46ab5;
    margin-bottom: 0.5rem;
  }
</style>
