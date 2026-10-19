<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import {
    ApiError,
    generateInvoices,
    type ClientRow,
    type EntityRow,
    type GenerateInvoicesRequest,
    type GenerateInvoicesResponse,
    type GeneratedInvoice,
  } from '$lib/money/api';
  import { Button, Field, Input, Modal, Select, type SelectOption } from '$lib/components/ui';
  import { formatDecimal as formatAmount } from '$lib/format';

  interface Props {
    clients?: ClientRow[];
    entities?: EntityRow[];
    /**
     * Bill exactly these entries, with the etags they were read at. The filter
     * step is skipped and the preview runs as soon as the dialog opens.
     */
    selection?: { entry_uids: string[]; etags: Record<string, string> } | null;
    /** Called after invoices were issued; `matchedPreview` false keeps the dialog open. */
    onIssued: (result: GenerateInvoicesResponse, matchedPreview: boolean) => void;
    onClose: () => void;
  }

  let { clients = [], entities = [], selection = null, onIssued, onClose }: Props = $props();

  // Mounted fresh each time it opens, so the opening selection is the whole story.
  const fixed = untrack(() => selection);

  let open = $state(true);
  let client = $state('');
  let entity = $state('');
  let period = $state('');
  let step: 'filters' | 'preview' | 'issued' = $state(fixed ? 'preview' : 'filters');
  let busy = $state(false);
  let error = $state('');
  let previewed = $state<GenerateInvoicesResponse | null>(null);
  let issued = $state<GenerateInvoicesResponse | null>(null);
  // The filters the preview ran with: confirming repeats exactly these.
  let previewRequest: Omit<GenerateInvoicesRequest, 'dry_run'> | null = null;

  const clientOptions = $derived<SelectOption[]>([
    { value: '', label: 'All clients' },
    ...clients.map((c) => ({ value: c.key, label: c.name || c.key })),
  ]);

  const entityOptions = $derived<SelectOption[]>([
    { value: '', label: 'All entities' },
    ...entities.map((e) => ({ value: e.key, label: e.name || e.key })),
  ]);

  function filterRequest(): Omit<GenerateInvoicesRequest, 'dry_run'> {
    if (fixed) return { entry_uids: fixed.entry_uids, etags: fixed.etags };
    const request: Omit<GenerateInvoicesRequest, 'dry_run'> = {};
    if (client) request.client = client;
    if (entity) request.entity = entity;
    if (period) request.period = period;
    return request;
  }

  function describeError(e: unknown): string {
    if (e instanceof ApiError && e.status === 409 && Array.isArray(e.payload?.stale)) {
      return 'This work changed since the page loaded. Close, reload and try again.';
    }
    return e instanceof Error ? e.message : 'Failed to generate invoices';
  }

  async function runPreview() {
    busy = true;
    error = '';
    previewed = null;
    const request = filterRequest();
    try {
      previewed = await generateInvoices({ ...request, dry_run: true });
      previewRequest = request;
      step = 'preview';
    } catch (e) {
      error = describeError(e);
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    if (fixed) runPreview();
  });

  function totalsOf(result: GenerateInvoicesResponse): number[] {
    return result.invoices.map((inv) => inv.total).sort((a, b) => a - b);
  }

  function sameAsPreview(preview: GenerateInvoicesResponse, result: GenerateInvoicesResponse) {
    if (preview.invoice_count !== result.invoice_count) return false;
    const a = totalsOf(preview);
    const b = totalsOf(result);
    return a.every((total, i) => Math.abs(total - b[i]) < 0.005);
  }

  async function confirm() {
    if (!previewed || !previewRequest || previewed.invoice_count === 0) return;
    busy = true;
    error = '';
    try {
      const result = await generateInvoices({ ...previewRequest, dry_run: false });
      const matched = sameAsPreview(previewed, result);
      if (matched) {
        open = false;
      } else {
        // Already issued, so no retry: say what happened and show it.
        issued = result;
        step = 'issued';
        error =
          `The issued invoices differ from the preview: ${result.invoice_count} issued ` +
          `for $${formatAmount(result.total)}, against ${previewed.invoice_count} previewed ` +
          `for $${formatAmount(previewed.total)}.`;
      }
      onIssued(result, matched);
    } catch (e) {
      error = describeError(e);
    } finally {
      busy = false;
    }
  }

  function handleOpenChange(next: boolean) {
    if (!next) onClose();
  }

  const shown: GeneratedInvoice[] = $derived((issued ?? previewed)?.invoices ?? []);
  const count = $derived(previewed?.invoice_count ?? 0);
  const issueLabel = $derived(`Issue ${count} invoice${count === 1 ? '' : 's'}`);
</script>

<Modal
  bind:open
  title={fixed ? 'Generate invoice' : 'Generate invoices'}
  onOpenChange={handleOpenChange}
  width="640px"
>
  {#if step === 'filters'}
    <div class="vstack">
      <Field label="Client" labelled={false}>
        <Select bind:value={client} options={clientOptions} fullWidth ariaLabel="Client" />
      </Field>
      <Field label="Entity" labelled={false}>
        <Select bind:value={entity} options={entityOptions} fullWidth ariaLabel="Entity" />
      </Field>
      <Field
        label="Period"
        hint="Bills unbilled work dated on or before the end of this month, earlier months included. Leave it empty to bill all unbilled work."
      >
        <Input type="month" bind:value={period} />
      </Field>
    </div>
  {:else if busy && !previewed}
    <p class="caption">Building the preview…</p>
  {:else if shown.length === 0 && previewed}
    <p class="caption">Nothing to invoice for these filters.</p>
  {:else if shown.length > 0}
    <div class="preview" role="table" aria-label="Invoices">
      <div class="preview-row preview-head caption" role="row">
        <span role="columnheader">Invoice</span>
        <span role="columnheader">Client</span>
        <span role="columnheader">Group</span>
        <span role="columnheader">Entries</span>
        <span role="columnheader" class="total">Total</span>
      </div>
      {#each shown as inv (inv.invoice_number)}
        <div class="preview-row" role="row">
          <span role="cell" class="number">{inv.invoice_number}</span>
          <span role="cell">{inv.client}</span>
          <span role="cell">{inv.group}</span>
          <span role="cell" class="entries">
            {#each inv.entry_descriptions as description, i (i)}
              <span>{description || '(no description)'}</span>
            {/each}
          </span>
          <span role="cell" class="total">${formatAmount(inv.total)}</span>
        </div>
      {/each}
    </div>
  {/if}

  {#if step !== 'filters'}
    <p class="caption notice">No email is sent to clients.</p>
  {/if}

  {#if error}<p class="form-error" role="alert">{error}</p>{/if}

  {#snippet footer()}
    {#if step === 'issued'}
      <Button variant="primary" onclick={onClose}>Close</Button>
    {:else}
      <Button variant="secondary" onclick={onClose}>Cancel</Button>
      {#if step === 'filters'}
        <Button variant="primary" onclick={runPreview} loading={busy} loadingLabel="Previewing…"
          >Preview</Button
        >
      {:else}
        {#if !fixed}
          <Button variant="secondary" onclick={() => (step = 'filters')} disabled={busy}
            >Back</Button
          >
        {/if}
        <Button
          variant="primary"
          onclick={confirm}
          disabled={busy || count === 0}
          loading={busy && !!previewed}
          loadingLabel="Issuing…">{issueLabel}</Button
        >
      {/if}
    {/if}
  {/snippet}
</Modal>

<style>
  .preview {
    display: flex;
    flex-direction: column;
  }

  .preview-row {
    display: grid;
    grid-template-columns: 6.5rem minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1.5fr) 5.5rem;
    gap: var(--space-2);
    padding: var(--space-1) 0;
    border-bottom: 1px solid var(--border-subtle);
    align-items: baseline;
  }

  .number {
    font-family: var(--font-mono);
    font-size: var(--text-xs);
  }

  .entries {
    display: flex;
    flex-direction: column;
    color: var(--text-secondary);
  }

  .total {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  .notice {
    margin: var(--space-3) 0 0;
  }

  .form-error {
    margin: var(--space-3) 0 0;
  }

  @media (max-width: 640px) {
    .preview-row {
      grid-template-columns: 6rem minmax(0, 1fr) 5rem;
    }
    .preview-row > :nth-child(2),
    .preview-row > :nth-child(3) {
      display: none;
    }
  }
</style>
