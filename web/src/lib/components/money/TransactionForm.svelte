<script lang="ts">
  import { untrack } from 'svelte';
  import type { TransactionRow } from '$lib/money/api';
  import { Modal, Button, Field, Input, Select, type SelectOption } from '$lib/components/ui';

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

  const focusOnMount = (el: HTMLElement) => el.focus();

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
    if (e.key !== 'Enter') return;
    // Only a single-line text input commits. Enter anywhere else is that
    // control's own business — notably confirming an account in the dropdown,
    // which would otherwise select *and* save in one keystroke.
    if (!(e.target instanceof HTMLInputElement)) return;
    handleSave();
  }
</script>

<svelte:window on:keydown={handleKeydown} />

<Modal bind:open title="Edit transaction" onOpenChange={handleOpenChange} width="380px">
  <div class="vstack">
    <Field label="Payee">
      <Input {@attach focusOnMount} bind:value={payee} placeholder="e.g. Acme Corp" />
    </Field>
    <Field label="Narration">
      <Input bind:value={narration} placeholder="Description" />
    </Field>
    <Field label="Date">
      <Input type="date" bind:value={date} />
    </Field>
    <Field label="Account">
      <Select bind:value={account} options={accountOptions} fullWidth />
    </Field>
    <Field label="Amount">
      <Input bind:value={position} placeholder="e.g. -12.50 USD" />
    </Field>
  </div>

  {#if error}
    <div class="form-error">{error}</div>
  {/if}

  {#snippet footer()}
    <Button variant="secondary" onclick={onCancel}>Cancel</Button>
    <Button variant="primary" onclick={handleSave} disabled={!account.trim() || saving}>
      {saving ? 'Saving…' : 'Save'}
    </Button>
  {/snippet}
</Modal>

<style>
  /* Type is the global .form-error; only the space around it is this form's. */
  .form-error {
    margin: var(--space-3) 0 var(--space-2);
  }
</style>
