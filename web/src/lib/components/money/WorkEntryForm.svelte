<script lang="ts">
  import { untrack } from 'svelte';
  import type { WorkEntryRow, WorkEntryInput, ClientRow, ServiceRow } from '$lib/money/api';
  import { Modal, Button, Select, type SelectOption } from '$lib/components/ui';

  interface Props {
    /** The entry being edited, or null when adding. */
    entry?: WorkEntryRow | null;
    clients?: ClientRow[];
    services?: ServiceRow[];
    onSave: (data: WorkEntryInput) => void;
    onCancel: () => void;
    error?: string;
    saving?: boolean;
  }

  let {
    entry = null,
    clients = [],
    services = [],
    onSave,
    onCancel,
    error = '',
    saving = false,
  }: Props = $props();

  // The form is mounted fresh each time it opens, so the initial props are
  // the whole story — the local fields below deliberately don't track them.
  const isEdit = untrack(() => !!entry);
  const today = new Date().toISOString().slice(0, 10);

  let date = $state(untrack(() => entry?.date ?? today));
  let client = $state(untrack(() => entry?.client ?? ''));
  let service = $state(untrack(() => entry?.service ?? ''));
  let qty = $state(untrack(() => (entry?.qty != null ? String(entry.qty) : '')));
  let amount = $state(untrack(() => (entry?.amount != null ? String(entry.amount) : '')));
  let discount = $state(untrack(() => (entry?.discount ? String(entry.discount) : '')));
  let description = $state(untrack(() => entry?.description ?? ''));
  let entity = $state(untrack(() => entry?.entity ?? ''));
  let open = $state(true);

  const clientOptions = $derived.by<SelectOption[]>(() => {
    const opts = clients.map((c) => ({ value: c.key, label: c.name || c.key }));
    // Keep an entry pointing at a since-removed client selectable rather than
    // silently reassigning it on the next save.
    if (client && !clients.some((c) => c.key === client)) {
      opts.push({ value: client, label: `${client} (unknown)` });
    }
    return opts;
  });

  const serviceOptions = $derived.by<SelectOption[]>(() => {
    const opts = services.map((s) => ({
      value: s.key,
      label: `${s.display_name || s.key} — ${formatRate(s)}`,
    }));
    if (service && !services.some((s) => s.key === service)) {
      opts.push({ value: service, label: `${service} (unknown)` });
    }
    return opts;
  });

  const selectedService = $derived(services.find((s) => s.key === service) ?? null);
  const selectedClient = $derived(clients.find((c) => c.key === client) ?? null);

  /** Which quantity field the service's rate rule actually reads. */
  const wants = $derived.by<'qty' | 'amount' | 'none'>(() => {
    const type = selectedService?.type ?? 'hours';
    if (type === 'flat') return 'none';
    if (type === 'other') return 'amount';
    return 'qty';
  });

  const entityOptions = $derived.by<SelectOption[]>(() => {
    const keys = new Set<string>();
    for (const c of clients) if (c.entity) keys.add(c.entity);
    if (entity) keys.add(entity);
    const opts = [...keys].sort().map((k) => ({ value: k, label: k }));
    return [{ value: '', label: 'Client default' }, ...opts];
  });

  function formatRate(s: ServiceRow): string {
    const unit = s.type === 'days' ? '/day' : s.type === 'hours' ? '/hr' : '';
    if (s.type === 'other') return 'custom amount';
    return `${formatMoney(s.rate)}${unit}`;
  }

  function formatMoney(value: number): string {
    return `$${value.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  }

  function num(raw: string): number | null {
    const trimmed = raw.trim();
    if (!trimmed) return null;
    const parsed = Number(trimmed);
    return Number.isFinite(parsed) ? parsed : null;
  }

  /**
   * Mirrors the backend's `entry_line_item` so the number is visible before
   * the entry is written, rather than at invoice time.
   */
  const preview = $derived.by(() => {
    const svc = selectedService;
    if (!svc) return '';
    const disc = num(discount) ?? 0;
    let q = 1;
    let rate = svc.rate;
    let subtotal: number;

    if (svc.type === 'other') {
      subtotal = num(amount) ?? 0;
      rate = subtotal;
    } else if (svc.type === 'flat') {
      subtotal = svc.rate;
    } else {
      q = num(qty) ?? 0;
      subtotal = q * rate;
      if (!subtotal && num(amount)) {
        subtotal = num(amount) as number;
        rate = subtotal;
        q = 1;
      }
    }

    const total = subtotal - disc;
    const head = svc.type === 'flat' ? formatMoney(rate) : `${q} × ${formatMoney(rate)}`;
    const tail = disc ? ` − ${formatMoney(disc)}` : '';
    return `${head}${tail} = ${formatMoney(total)}`;
  });

  const canSave = $derived(!!date && !!client && !!service && !saving);

  function handleSave() {
    if (!canSave) return;
    onSave({
      date,
      client,
      service,
      qty: wants === 'qty' ? num(qty) : null,
      amount: wants === 'amount' ? num(amount) : null,
      discount: num(discount) ?? 0,
      description: description.trim(),
      entity,
    });
  }

  function handleOpenChange(next: boolean) {
    if (!next) onCancel();
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key !== 'Enter') return;
    const target = e.target as HTMLElement | null;
    // Only a text/date field commits the form. Enter anywhere else is that
    // control's own business — notably confirming a dropdown option, which
    // would otherwise select *and* save in one keystroke, writing a
    // half-filled entry.
    if (!(target instanceof HTMLInputElement)) return;
    // A stray Enter while typing prose shouldn't commit either.
    if (target.dataset.multiline === 'true') return;
    handleSave();
  }
</script>

<svelte:window on:keydown={handleKeydown} />

<Modal
  bind:open
  title={isEdit ? 'Edit work entry' : 'Add work entry'}
  onOpenChange={handleOpenChange}
  width="400px"
>
  <label class="field">
    <span>Date</span>
    <input type="date" bind:value={date} />
  </label>

  <label class="field">
    <span>Client</span>
    <Select bind:value={client} options={clientOptions} fullWidth ariaLabel="Client" />
  </label>

  <label class="field">
    <span>Service</span>
    <Select bind:value={service} options={serviceOptions} fullWidth ariaLabel="Service" />
  </label>

  {#if wants === 'qty'}
    <label class="field">
      <span>{selectedService?.type === 'days' ? 'Days' : 'Hours'}</span>
      <input type="text" inputmode="decimal" bind:value={qty} placeholder="e.g. 3" />
    </label>
  {:else if wants === 'amount'}
    <label class="field">
      <span>Amount</span>
      <input type="text" inputmode="decimal" bind:value={amount} placeholder="e.g. 1200" />
    </label>
  {:else if selectedService}
    <p class="field-note">Flat-rate service — quantity is not used.</p>
  {/if}

  <label class="field">
    <span>Discount</span>
    <input type="text" inputmode="decimal" bind:value={discount} placeholder="0" />
  </label>

  <label class="field">
    <span>Description</span>
    <input type="text" data-multiline="true" bind:value={description} placeholder="Optional" />
  </label>

  <label class="field">
    <span>Entity</span>
    <Select bind:value={entity} options={entityOptions} fullWidth ariaLabel="Entity" />
    {#if !entity && selectedClient?.entity}
      <span class="field-hint">Defaults to {selectedClient.entity}</span>
    {/if}
  </label>

  {#if preview}
    <div class="preview">{preview}</div>
  {/if}

  {#if error}
    <div class="form-error">{error}</div>
  {/if}

  {#snippet footer()}
    <Button variant="ghost" onclick={onCancel}>Cancel</Button>
    <Button variant="primary" onclick={handleSave} disabled={!canSave}>
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

  .field-hint {
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .field-note {
    font-size: var(--text-xs);
    color: var(--text-dim);
    margin: 0 0 0.75rem;
  }

  .preview {
    font-size: var(--text-sm);
    font-variant-numeric: tabular-nums;
    color: var(--text-secondary);
    background: var(--surface-card);
    border-radius: 0.25rem;
    padding: 0.4rem 0.5rem;
    margin-bottom: 0.5rem;
  }

  .form-error {
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
    margin-bottom: 0.5rem;
  }
</style>
