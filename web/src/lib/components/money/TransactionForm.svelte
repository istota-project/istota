<script lang="ts">
  import { untrack } from 'svelte';
  import type { TransactionRow } from '$lib/money/api';
  import { FormModal, Field, Input, Select, type SelectOption } from '@minot/ui';

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
    }) => void | Promise<void>;
    onCancel: () => void;
  }

  let { txn, accounts = [], onSave, onCancel }: Props = $props();

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
    return onSave({
      payee: payee.trim(),
      narration: narration.trim(),
      date: date.trim(),
      account: account.trim(),
      position: position.trim(),
    });
  }
</script>

<FormModal
  bind:open
  title="Edit transaction"
  width="380px"
  canSubmit={!!account.trim()}
  onSubmit={handleSave}
  {onCancel}
>
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
</FormModal>
