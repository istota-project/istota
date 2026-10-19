<script lang="ts">
  import { untrack } from 'svelte';
  import { KEY_RE, KEY_HINT, type ServiceRow, type ServiceInput } from '$lib/money/api';
  import { Modal, Button, Select, type SelectOption, Field } from '$lib/components/ui';

  /**
   * Create/edit form for a billable service.
   *
   * `type` is a closed set because `entry_line_item` branches on it: `other`
   * takes the amount off each work entry, `flat` bills the rate once, and
   * everything else multiplies quantity by rate. A value outside the set has
   * no branch and silently bills as hours — so the field is a dropdown here
   * and a rejected value server-side, never a free-text box.
   */
  interface Props {
    service?: ServiceRow | null;
    onSave: (key: string, data: ServiceInput) => void;
    onCancel: () => void;
    error?: string;
    saving?: boolean;
  }

  let { service = null, onSave, onCancel, error = '', saving = false }: Props = $props();

  const isEdit = untrack(() => !!service);

  let key = $state(untrack(() => service?.key ?? ''));
  let displayName = $state(untrack(() => service?.display_name ?? ''));
  let type = $state(untrack(() => service?.type || 'hours'));
  let rate = $state(untrack(() => (service?.rate != null ? String(service.rate) : '')));
  let incomeAccount = $state(untrack(() => service?.income_account ?? ''));
  let open = $state(true);

  const KNOWN_TYPES = ['hours', 'days', 'flat', 'other'];
  const baseTypeOptions: SelectOption[] = [
    { value: 'hours', label: 'Hourly' },
    { value: 'days', label: 'Daily' },
    { value: 'flat', label: 'Flat rate' },
    { value: 'other', label: 'Variable (per entry)' },
  ];

  // A service migrated from legacy TOML can carry a type outside the set (it
  // billed as hours, silently). Showing it as its own option means the value
  // is visible and re-saving it is a no-op the server grandfathers, instead of
  // the dropdown quietly reading "Hourly" for a record that isn't.
  const legacyType = untrack(() =>
    service?.type && !KNOWN_TYPES.includes(service.type) ? service.type : '',
  );
  const typeOptions: SelectOption[] = legacyType
    ? [...baseTypeOptions, { value: legacyType, label: `${legacyType} (unrecognised)` }]
    : baseTypeOptions;

  const rateLabel = $derived(
    type === 'days' ? 'Rate per day' : type === 'flat' ? 'Flat rate' : 'Rate per hour',
  );

  const keyError = $derived(!isEdit && key && !KEY_RE.test(key) ? KEY_HINT : '');
  const rateError = $derived.by(() => {
    if (type === 'other' || !rate.trim()) return '';
    const parsed = Number(rate.trim());
    return Number.isFinite(parsed) && parsed >= 0 ? '' : 'Expected an amount of 0 or more';
  });
  const typeWarning = $derived(
    legacyType && type === legacyType
      ? `"${legacyType}" is not a billing type — it bills as hours. Pick one to fix it.`
      : '',
  );
  const canSave = $derived(
    !!displayName.trim() && (isEdit || (!!key && !keyError)) && !rateError && !saving,
  );

  function handleSave() {
    if (!canSave) return;
    const data: ServiceInput = {
      display_name: displayName.trim(),
      type,
      income_account: incomeAccount.trim(),
    };
    // Omit rather than zero when the box is blank. An "other" service prices
    // off each work entry so the box isn't shown at all, and a cleared box on
    // any other type would store 0 — which, because the invoice list rebuilds
    // totals from live config, silently reprices every past invoice carrying
    // this service to nothing.
    if (type !== 'other' && rate.trim()) data.rate = Number(rate.trim());
    onSave(isEdit ? (service as ServiceRow).key : key.trim(), data);
  }

  function handleOpenChange(next: boolean) {
    if (!next) onCancel();
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key !== 'Enter') return;
    if (!(e.target instanceof HTMLInputElement)) return;
    handleSave();
  }
</script>

<svelte:window on:keydown={handleKeydown} />

<Modal
  bind:open
  title={isEdit ? `Edit ${service?.display_name || service?.key}` : 'Add service'}
  onOpenChange={handleOpenChange}
  width="400px"
>
  <div class="vstack" style:--vstack-gap="var(--space-2)">
    {#if isEdit}
      <Field label="Key" description="The key is the identity — work entries reference it by name.">
        <code class="key-value">{service?.key}</code>
      </Field>
    {:else}
      <Field label="Key" hint="Short identifier used by work entries." error={keyError}>
        <input type="text" bind:value={key} placeholder="consulting" autocomplete="off" />
      </Field>
    {/if}

    <Field label="Display name" hint="Printed on the invoice line.">
      <input type="text" bind:value={displayName} placeholder="Consulting" />
    </Field>

    <Field label="Type" warning={typeWarning}>
      <Select bind:value={type} options={typeOptions} fullWidth ariaLabel="Service type" />
    </Field>

    {#if type === 'other'}
      <p class="field-note caption">Rate is not used — the amount comes from each work entry.</p>
    {:else}
      <Field label={rateLabel} error={rateError}>
        <input type="text" inputmode="decimal" bind:value={rate} placeholder="150" />
      </Field>
    {/if}

    <Field label="Income account">
      <input type="text" bind:value={incomeAccount} placeholder="Income:Consulting" />
    </Field>
  </div>

  {#if error}
    <div class="form-error">{error}</div>
  {/if}

  {#snippet footer()}
    <Button variant="secondary" onclick={onCancel}>Cancel</Button>
    <Button variant="primary" onclick={handleSave} disabled={!canSave}>
      {saving ? 'Saving…' : 'Save'}
    </Button>
  {/snippet}
</Modal>

<style>
  .key-value {
    color: var(--text-secondary);
  }

  .field-note {
    margin: 0;
  }

  /* Type is the global .form-error; only the space above it is this form's. */
  .form-error {
    margin-top: var(--space-2);
  }
</style>
