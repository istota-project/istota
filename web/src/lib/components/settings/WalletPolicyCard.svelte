<script lang="ts">
  import { untrack } from 'svelte';
  import { AuthError, saveWalletPolicy, type WalletPolicy } from '$lib/api';
  import { Field, Input, Select } from '$lib/components/ui';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';
  import SettingsCard from './SettingsCard.svelte';
  let {
    policy,
    writable,
    onError,
  }: { policy: WalletPolicy; writable: boolean; onError: (e: unknown) => void } = $props();
  const start = untrack(() => policy);
  function exponent(code: string) {
    return (
      new Intl.NumberFormat('en', { style: 'currency', currency: code }).resolvedOptions()
        .maximumFractionDigits ?? 2
    );
  }
  function amount(value: number | null) {
    return value === null
      ? ''
      : (value / 10 ** exponent(start.currency)).toFixed(exponent(start.currency));
  }
  let currency = $state(start.currency);
  let limit = $state(amount(start.auto_limit_cents));
  let budget = $state(amount(start.auto_budget_cents));
  let ceiling = $state(amount(start.ceiling_cents));
  let scheduled = $state(start.allow_scheduled);
  let saving = $state(false);
  let error = $state('');
  function snapshot() {
    return JSON.stringify([currency, limit, budget, ceiling, scheduled]);
  }
  let initial = $state(snapshot());
  let dirty = $derived(snapshot() !== initial);
  const currencies = [...new Set([start.currency, ...Intl.supportedValuesOf('currency')])]
    .sort()
    .map((value) => ({ value, label: value }));
  function minor(text: string) {
    const digits = exponent(currency);
    if (
      !/^\d+(\.\d+)?$/.test(text) ||
      (text.split('.')[1]?.replace(/0+$/, '').length ?? 0) > digits
    )
      throw new Error('Use a non-negative amount with the currency’s number of decimal places.');
    const [whole, fraction = ''] = text.split('.');
    const value = Number(whole + fraction.slice(0, digits).padEnd(digits, '0'));
    if (!Number.isSafeInteger(value)) throw new Error('That amount is too large.');
    return value;
  }
  async function save() {
    if (!writable || saving) return;
    saving = true;
    error = '';
    try {
      await saveWalletPolicy({
        currency,
        auto_limit_cents: minor(limit),
        auto_budget_cents: minor(budget),
        ceiling_cents: ceiling === '' ? null : minor(ceiling),
        allow_scheduled: scheduled,
      });
      initial = snapshot();
    } catch (e) {
      error = (e as Error).message;
      if (e instanceof AuthError) onError(e);
    } finally {
      saving = false;
    }
  }
  useSettingsSave(() => (writable ? { dirty, saving, save } : null));
</script>

<SettingsCard
  title="Spending policy"
  description="Purchases above the auto limits wait for your approval. Purchases above the ceiling are refused."
>
  <fieldset class="policy-form" disabled={!writable || saving}>
    <Field label="Currency" labelled={false}
      ><Select
        bind:value={currency}
        options={currencies}
        ariaLabel="Currency"
        fullWidth
        disabled={!writable || saving}
      /></Field
    >
    <Field label="Auto limit per purchase"><Input bind:value={limit} inputmode="decimal" /></Field>
    <Field label="30-day auto budget"><Input bind:value={budget} inputmode="decimal" /></Field>
    <Field label="Ceiling per purchase" hint="Leave empty for no ceiling."
      ><Input bind:value={ceiling} inputmode="decimal" /></Field
    >
    <Field label="Scheduled tasks may spend automatically" checkbox
      ><input type="checkbox" bind:checked={scheduled} /></Field
    >
  </fieldset>
  {#if error}<p class="banner error" role="alert">{error}</p>{/if}
</SettingsCard>

<style>
  .policy-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    min-width: 0;
    margin: 0;
    padding: 0;
    border: 0;
  }
</style>
