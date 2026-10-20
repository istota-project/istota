<script lang="ts">
  import { untrack } from 'svelte';
  import { AuthError, saveWalletPolicy, type WalletPolicy, type WalletSettings } from '$lib/api';
  import { Alert, Field, FormStack, Input, Select, Card, Checkbox } from '$lib/components/ui';
  import { createDraft } from '$lib/ui/draft.svelte';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';
  let {
    policy,
    precision,
    writable,
    onError,
  }: {
    policy: WalletPolicy;
    precision: WalletSettings['currency_precision'];
    writable: boolean;
    onError: (e: unknown) => void;
  } = $props();
  const start = untrack(() => policy);
  function exponent(code: string) {
    return precision.exceptions[code] ?? precision.default;
  }
  function amount(value: number | null) {
    return value === null
      ? ''
      : (value / 10 ** exponent(start.currency)).toFixed(exponent(start.currency));
  }
  const form = createDraft({
    currency: start.currency,
    limit: amount(start.auto_limit_cents),
    budget: amount(start.auto_budget_cents),
    ceiling: amount(start.ceiling_cents),
    scheduled: start.allow_scheduled,
  });
  let saving = $state(false);
  let error = $state('');
  const currencies = [...new Set([start.currency, ...Intl.supportedValuesOf('currency')])]
    .sort()
    .map((value) => ({ value, label: value }));
  function minor(text: string) {
    const digits = exponent(form.value.currency);
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
    const { currency, limit, budget, ceiling, scheduled } = form.value;
    try {
      await saveWalletPolicy({
        currency,
        auto_limit_cents: minor(limit),
        auto_budget_cents: minor(budget),
        ceiling_cents: ceiling === '' ? null : minor(ceiling),
        allow_scheduled: scheduled,
      });
      form.reset();
    } catch (e) {
      error = (e as Error).message;
      if (e instanceof AuthError) onError(e);
    } finally {
      saving = false;
    }
  }
  useSettingsSave(() => (writable ? { dirty: form.dirty, saving, save } : null));
</script>

<Card
  title="Spending policy"
  description="Purchases above the auto limits wait for your approval. Purchases above the ceiling are refused."
>
  <FormStack disabled={!writable || saving}>
    <Field label="Currency"
      ><Select
        bind:value={form.value.currency}
        options={currencies}
        ariaLabel="Currency"
        fullWidth
        disabled={!writable || saving}
      /></Field
    >
    <Field label="Auto limit per purchase"
      ><Input bind:value={form.value.limit} inputmode="decimal" /></Field
    >
    <Field label="30-day auto budget"
      ><Input bind:value={form.value.budget} inputmode="decimal" /></Field
    >
    <Field label="Ceiling per purchase" hint="Leave empty for no ceiling."
      ><Input bind:value={form.value.ceiling} inputmode="decimal" /></Field
    >
    <Field label="Scheduled tasks may spend automatically" checkbox
      ><Checkbox bind:checked={form.value.scheduled} /></Field
    >
  </FormStack>
  {#if error}<Alert appearance="inline" variant="danger" role="alert">{error}</Alert>{/if}
</Card>
