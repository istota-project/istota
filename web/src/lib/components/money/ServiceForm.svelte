<script lang="ts">
  import { untrack } from 'svelte';
  import type { ServiceRow, ServiceInput } from '$lib/money/api';
  import { Modal, Button, Select, type SelectOption } from '$lib/components/ui';
  import { SettingsField } from '$lib/components/settings';

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

  const typeOptions: SelectOption[] = [
    { value: 'hours', label: 'Hourly' },
    { value: 'days', label: 'Daily' },
    { value: 'flat', label: 'Flat rate' },
    { value: 'other', label: 'Variable (per entry)' },
  ];

  const rateLabel = $derived(
    type === 'days' ? 'Rate per day' : type === 'flat' ? 'Flat rate' : 'Rate per hour',
  );

  const KEY_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
  const keyError = $derived(
    !isEdit && key && !KEY_RE.test(key) ? 'Letters, digits, - and _ only' : '',
  );
  const rateError = $derived.by(() => {
    if (type === 'other' || !rate.trim()) return '';
    const parsed = Number(rate.trim());
    return Number.isFinite(parsed) && parsed >= 0 ? '' : 'Expected an amount of 0 or more';
  });
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
    // An "other" service prices off each work entry, so the form shows no
    // rate box — leave whatever is stored alone rather than zeroing it.
    if (type !== 'other') data.rate = Number(rate.trim() || 0);
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
  <div class="form-grid">
    {#if isEdit}
      <div class="static-key">
        <span>Key</span>
        <code>{service?.key}</code>
        <small>The key is the identity — work entries reference it by name.</small>
      </div>
    {:else}
      <SettingsField label="Key" hint="Short identifier used by work entries." error={keyError}>
        <input type="text" bind:value={key} placeholder="consulting" autocomplete="off" />
      </SettingsField>
    {/if}

    <SettingsField label="Display name" hint="Printed on the invoice line.">
      <input type="text" bind:value={displayName} placeholder="Consulting" />
    </SettingsField>

    <SettingsField label="Type">
      <Select bind:value={type} options={typeOptions} fullWidth ariaLabel="Service type" />
    </SettingsField>

    {#if type === 'other'}
      <p class="field-note">Rate is not used — the amount comes from each work entry.</p>
    {:else}
      <SettingsField label={rateLabel} error={rateError}>
        <input type="text" inputmode="decimal" bind:value={rate} placeholder="150" />
      </SettingsField>
    {/if}

    <SettingsField label="Income account">
      <input type="text" bind:value={incomeAccount} placeholder="Income:Consulting" />
    </SettingsField>
  </div>

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
  .form-grid {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }

  .static-key {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    font-size: var(--text-sm);
  }

  .static-key > span {
    color: var(--text-muted);
  }

  .static-key code {
    font-size: var(--text-xs);
    color: var(--text-secondary);
  }

  .static-key small {
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  .field-note {
    font-size: var(--text-xs);
    color: var(--text-dim);
    margin: 0;
  }

  .form-error {
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
    margin-top: 0.5rem;
  }
</style>
