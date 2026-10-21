<script lang="ts">
  import {
    createInvoice,
    type ClientRow,
    type CreateInvoiceResponse,
    type EntityRow,
    type ManualInvoiceItem,
    type ServiceRow,
  } from '$lib/money/api';
  import { quantityFieldFor } from '$lib/money/workEntryPayload';
  import {
    Button,
    Field,
    FormModal,
    IconButton,
    Input,
    Select,
    type SelectOption,
  } from '@minot/ui';
  import { X } from '@lucide/svelte';

  interface Props {
    clients?: ClientRow[];
    entities?: EntityRow[];
    services?: ServiceRow[];
    onCreated: (result: CreateInvoiceResponse) => void;
    onCancel: () => void;
  }

  let { clients = [], entities = [], services = [], onCreated, onCancel }: Props = $props();

  interface Line {
    id: number;
    /** A configured service billed by quantity, or '' for an amount typed here. */
    service: string;
    qty: string;
    amount: string;
    description: string;
  }

  let nextId = 1;
  function blankLine(): Line {
    return { id: nextId++, service: '', qty: '', amount: '', description: '' };
  }

  let open = $state(true);
  let client = $state('');
  let entity = $state('');
  let lines: Line[] = $state([blankLine()]);

  const clientOptions = $derived<SelectOption[]>(
    clients.map((c) => ({ value: c.key, label: c.name || c.key })),
  );

  const entityOptions = $derived<SelectOption[]>([
    { value: '', label: 'Client default' },
    ...entities.map((e) => ({ value: e.key, label: e.name || e.key })),
  ]);

  // A service line carries a quantity only, the same as `invoice create
  // --service`; an amount-priced ("other") service has nothing to bill that
  // way, so it is entered as an amount line instead.
  const serviceOptions = $derived<SelectOption[]>([
    { value: '', label: 'Amount' },
    ...services
      .filter((s) => quantityFieldFor(s.type) !== 'amount')
      .map((s) => ({ value: s.key, label: s.display_name || s.key })),
  ]);

  function serviceOf(line: Line): ServiceRow | undefined {
    return services.find((s) => s.key === line.service);
  }

  function num(raw: string): number | null {
    const trimmed = raw.trim();
    if (!trimmed) return null;
    const parsed = Number(trimmed);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function itemOf(line: Line): ManualInvoiceItem | null {
    const description = line.description.trim();
    if (!line.service) {
      const amount = num(line.amount);
      return amount == null ? null : { description, amount };
    }
    if (quantityFieldFor(serviceOf(line)?.type) === 'none') {
      return { service: line.service, description };
    }
    const qty = num(line.qty);
    return qty == null ? null : { service: line.service, qty, description };
  }

  const items = $derived(lines.map(itemOf));
  const canCreate = $derived(!!client && items.length > 0 && items.every((item) => item !== null));

  function addLine() {
    lines = [...lines, blankLine()];
  }

  function removeLine(id: number) {
    lines = lines.filter((line) => line.id !== id);
  }

  async function submit() {
    const body: Parameters<typeof createInvoice>[0] = {
      client,
      items: items as ManualInvoiceItem[],
    };
    if (entity) body.entity = entity;
    onCreated(await createInvoice(body));
  }
</script>

<FormModal
  bind:open
  title="New manual invoice"
  width="560px"
  submitLabel="Create invoice"
  busyLabel="Creating…"
  canSubmit={canCreate}
  onSubmit={submit}
  {onCancel}
>
  <div class="head">
    <Field label="Client">
      <Select bind:value={client} options={clientOptions} fullWidth ariaLabel="Client" />
    </Field>
    <Field label="Entity">
      <Select bind:value={entity} options={entityOptions} fullWidth ariaLabel="Entity" />
    </Field>
  </div>

  <span class="micro-label">Lines</span>
  {#each lines as line, i (line.id)}
    {@const wants = line.service ? quantityFieldFor(serviceOf(line)?.type) : 'amount'}
    <div class="line">
      <Field label="Bill by">
        <Select
          bind:value={line.service}
          options={serviceOptions}
          fullWidth
          ariaLabel="Bill by, line {i + 1}"
        />
      </Field>
      {#if wants === 'amount'}
        <Field label="Amount">
          <Input bind:value={line.amount} inputmode="decimal" placeholder="e.g. 1200" />
        </Field>
      {:else if wants === 'qty'}
        <Field label={serviceOf(line)?.type === 'days' ? 'Days' : 'Hours'}>
          <Input bind:value={line.qty} inputmode="decimal" placeholder="e.g. 3" />
        </Field>
      {:else}
        <p class="caption flat">Flat rate</p>
      {/if}
      <Field label="Description" class="line-description">
        <Input bind:value={line.description} placeholder="Optional" />
      </Field>
      {#if lines.length > 1}
        <IconButton label="Remove line {i + 1}" onclick={() => removeLine(line.id)} icon={X} />
      {/if}
    </div>
  {/each}
  <div class="add-line">
    <Button variant="secondary" size="sm" onclick={addLine}>Add line</Button>
  </div>

  <p class="caption">No email is sent to clients.</p>
</FormModal>

<style>
  .head {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--space-3);
    margin-bottom: var(--space-4);
  }

  .line {
    display: grid;
    grid-template-columns: 8rem 6rem minmax(0, 1fr) auto;
    gap: var(--space-2);
    align-items: end;
    margin-top: var(--space-2);
  }

  .flat {
    margin: 0;
    align-self: center;
  }

  .add-line {
    margin: var(--space-3) 0;
  }

  @media (max-width: 640px) {
    .head {
      grid-template-columns: 1fr;
    }
    .line {
      grid-template-columns: 1fr 1fr auto;
    }
    .line :global(.line-description) {
      grid-column: 1 / -1;
      grid-row: 2;
    }
  }
</style>
