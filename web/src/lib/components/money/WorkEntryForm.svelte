<script lang="ts">
  import { untrack } from 'svelte';
  import type { WorkEntryRow, ClientRow, ServiceRow } from '$lib/money/api';
  import {
    buildWorkEntryPayload,
    quantityFieldFor,
    type WorkEntrySavePayload,
  } from '$lib/money/workEntryPayload';
  import { FormModal, Field, Input, Select, type SelectOption, Checkbox } from '$lib/components/ui';
  import { formatDecimal } from '$lib/format';
  import { localDateISO } from '$lib/dateFormat';

  interface Props {
    /** The entry being edited, or null when adding. */
    entry?: WorkEntryRow | null;
    clients?: ClientRow[];
    services?: ServiceRow[];
    onSave: (data: WorkEntrySavePayload) => void | Promise<void>;
    onCancel: () => void;
  }

  let { entry = null, clients = [], services = [], onSave, onCancel }: Props = $props();

  // The form is mounted fresh each time it opens, so the initial props are
  // the whole story — the local fields below deliberately don't track them.
  const isEdit = untrack(() => !!entry);
  // The service the entry arrived with: switching away from it is the one
  // case where clearing the other quantity field is right. See buildWorkEntryPayload.
  const initialService = untrack(() => entry?.service ?? '');
  // Local, not UTC: toISOString() renders the *UTC* date, so a user west of
  // Greenwich logging work in the evening got tomorrow pre-filled onto a
  // billable record — and the list below renders dates in local time, so the
  // field visibly disagreed with "today".
  const today = localDateISO();

  let date = $state(untrack(() => entry?.date ?? today));
  let client = $state(untrack(() => entry?.client ?? ''));
  let service = $state(untrack(() => entry?.service ?? ''));
  let qty = $state(untrack(() => (entry?.qty != null ? String(entry.qty) : '')));
  let amount = $state(untrack(() => (entry?.amount != null ? String(entry.amount) : '')));
  let discount = $state(untrack(() => (entry?.discount ? String(entry.discount) : '')));
  let description = $state(untrack(() => entry?.description ?? ''));
  let entity = $state(untrack(() => entry?.entity ?? ''));
  let separate = $state(untrack(() => entry?.separate ?? false));
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
  const wants = $derived(quantityFieldFor(selectedService?.type));

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

  const formatMoney = (value: number) => `$${formatDecimal(value)}`;

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

  const canSave = $derived(!!date && !!client && !!service);

  function handleSave() {
    return onSave(
      buildWorkEntryPayload({
        date,
        client,
        service,
        initialService,
        wants,
        qty: num(qty),
        amount: num(amount),
        discount: num(discount) ?? 0,
        description: description.trim(),
        entity,
        separate,
      }),
    );
  }
</script>

<FormModal
  bind:open
  title={isEdit ? 'Edit work entry' : 'Add work entry'}
  width="400px"
  canSubmit={canSave}
  onSubmit={handleSave}
  {onCancel}
>
  <div class="vstack">
    <Field label="Date">
      <Input type="date" bind:value={date} />
    </Field>

    <Field label="Client">
      <Select bind:value={client} options={clientOptions} fullWidth />
    </Field>

    <Field label="Service">
      <Select bind:value={service} options={serviceOptions} fullWidth />
    </Field>

    {#if wants === 'qty'}
      <Field label={selectedService?.type === 'days' ? 'Days' : 'Hours'}>
        <Input inputmode="decimal" bind:value={qty} placeholder="e.g. 3" />
      </Field>
    {:else if wants === 'amount'}
      <Field label="Amount">
        <Input inputmode="decimal" bind:value={amount} placeholder="e.g. 1200" />
      </Field>
    {:else if selectedService}
      <p class="caption">Flat-rate service — quantity is not used.</p>
    {/if}

    <Field label="Discount">
      <Input inputmode="decimal" bind:value={discount} placeholder="0" />
    </Field>

    <Field label="Description" optional>
      <Input bind:value={description} />
    </Field>

    <Field
      label="Entity"
      description={!entity && selectedClient?.entity
        ? `Defaults to ${selectedClient.entity}`
        : undefined}
    >
      <Select bind:value={entity} options={entityOptions} fullWidth />
    </Field>

    <Field
      label="Invoice separately"
      checkbox
      hint="This entry always gets an invoice of its own, whatever the client's bundling rules say."
    >
      <Checkbox bind:checked={separate} />
    </Field>
  </div>

  {#if preview}
    <div class="preview">{preview}</div>
  {/if}
</FormModal>

<style>
  p {
    margin: 0;
  }

  .preview {
    font-size: var(--text-sm);
    font-variant-numeric: tabular-nums;
    color: var(--text-secondary);
    background: var(--surface-card);
    border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-2);
    margin: var(--space-3) 0 var(--space-2);
  }
</style>
