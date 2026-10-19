<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import {
    ApiError,
    getInvoiceDetails,
    voidInvoice,
    type InvoiceDetailItem,
    type VoidInvoiceResponse,
  } from '$lib/money/api';
  import { Button, Field, Input, Modal, Checkbox } from '$lib/components/ui';
  import { formatDecimal as formatAmount } from '$lib/format';

  interface Props {
    invoiceNumber: string;
    /** A paid invoice needs the second checkbox before it can be voided. */
    paid?: boolean;
    onVoided: (result: VoidInvoiceResponse) => void;
    onCancel: () => void;
  }

  let { invoiceNumber, paid = false, onVoided, onCancel }: Props = $props();

  // Mounted fresh for each void, so the props it opened with are the whole story.
  const number = untrack(() => invoiceNumber);

  let open = $state(true);
  let reason = $state('');
  let replacedBy = $state('');
  let force = $state(false);
  // The server's answer wins over the row: a payment recorded since the page
  // loaded comes back as a 409 with code `paid`.
  let needsForce = $state(untrack(() => paid));
  let submitting = $state(false);
  let error = $state('');

  let lines: InvoiceDetailItem[] = $state([]);
  let linesState: 'loading' | 'ready' | 'failed' = $state('loading');

  onMount(() => {
    getInvoiceDetails(number)
      .then((resp) => {
        lines = resp.items;
        linesState = 'ready';
      })
      .catch(() => {
        linesState = 'failed';
      });
  });

  function replacementNumbers(): string[] {
    return replacedBy
      .split(',')
      .map((part) => part.trim())
      .filter((part) => part.length > 0);
  }

  const canVoid = $derived(!submitting && (!needsForce || force));

  async function submit() {
    if (!canVoid) return;
    submitting = true;
    error = '';
    try {
      const result = await voidInvoice(number, {
        reason: reason.trim(),
        replaced_by: replacementNumbers(),
        force: needsForce && force,
      });
      open = false;
      onVoided(result);
    } catch (e) {
      if (e instanceof ApiError && e.payload?.code === 'paid') needsForce = true;
      error = e instanceof Error ? e.message : 'Failed to void the invoice';
    } finally {
      submitting = false;
    }
  }

  function handleOpenChange(next: boolean) {
    if (!next) onCancel();
  }
</script>

<Modal bind:open title="Void {number}" onOpenChange={handleOpenChange} width="440px">
  <p class="intro">
    {number} stays on record as voided and its number is not reused. The work it billed goes back to unbilled.
  </p>

  <div class="released">
    <span class="micro-label">Work this releases</span>
    {#if linesState === 'loading'}
      <p class="caption">Loading…</p>
    {:else if linesState === 'failed'}
      <p class="caption">Could not load the invoice's lines.</p>
    {:else if lines.length === 0}
      <p class="caption">No work entries are on this invoice.</p>
    {:else}
      <ul>
        {#each lines as line, i (i)}
          <li>
            <span>{line.description}{line.detail ? ` — ${line.detail}` : ''}</span>
            <span class="amount">${formatAmount(line.amount)}</span>
          </li>
        {/each}
      </ul>
    {/if}
  </div>

  <div class="vstack">
    <Field label="Reason">
      <Input bind:value={reason} placeholder="Optional" />
    </Field>
    <Field label="Replaced by" hint="Invoice numbers, separated by commas.">
      <Input bind:value={replacedBy} placeholder="INV-000253, INV-000254" monospace />
    </Field>
    {#if needsForce}
      <Field label="This invoice was paid. Void anyway" checkbox>
        <Checkbox bind:checked={force} />
      </Field>
    {/if}
  </div>

  {#if error}<p class="form-error" role="alert">{error}</p>{/if}

  {#snippet footer()}
    <Button variant="secondary" onclick={onCancel}>Cancel</Button>
    <Button
      variant="danger"
      onclick={submit}
      disabled={!canVoid}
      loading={submitting}
      loadingLabel="Voiding…">Void invoice</Button
    >
  {/snippet}
</Modal>

<style>
  .intro {
    margin: 0 0 var(--space-3);
    color: var(--text-secondary);
  }

  .released {
    margin-bottom: var(--space-3);
  }

  .released p {
    margin: var(--space-1) 0 0;
  }

  .released ul {
    list-style: none;
    margin: var(--space-1) 0 0;
    padding: 0;
  }

  .released li {
    display: flex;
    justify-content: space-between;
    gap: var(--space-3);
    padding: var(--space-1) 0;
    border-bottom: 1px solid var(--border-subtle);
  }

  .amount {
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .form-error {
    margin: var(--space-3) 0 0;
  }
</style>
