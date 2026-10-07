<script lang="ts">
  import { onDestroy, untrack } from 'svelte';
  import {
    AuthError,
    addWalletCard,
    updateWalletCard,
    type WalletCard,
    type WalletBilling,
  } from '$lib/api';
  import { Button, Field, Input, Modal } from '$lib/components/ui';
  let {
    card = null,
    onClose,
    onSaved,
    onError,
  }: {
    card?: WalletCard | null;
    onClose: () => void;
    onSaved: () => void;
    onError: (e: unknown) => void;
  } = $props();
  const start = untrack(() => card);
  let open = $state(true);
  let busy = $state(false);
  let banner = $state('');
  let errors: Record<string, string> = $state({});
  let label = $state(start?.label ?? '');
  let number = $state('');
  let cvc = $state('');
  let month = $state(String(start?.exp_month ?? ''));
  let year = $state(String(start?.exp_year ?? ''));
  let name = $state(start?.name ?? '');
  let billing: WalletBilling = $state({
    line1: '',
    line2: '',
    city: '',
    region: '',
    postcode: '',
    country: '',
    ...start?.billing,
  });
  const billingLabels: Record<keyof WalletBilling, string> = {
    line1: 'Address line 1',
    line2: 'Address line 2',
    city: 'City',
    region: 'Region',
    postcode: 'Postcode',
    country: 'Country',
  };
  function clearSecrets() {
    number = '';
    cvc = '';
  }
  onDestroy(clearSecrets);
  function close() {
    if (busy) {
      open = true;
      return;
    }
    clearSecrets();
    onClose();
  }
  async function save() {
    if (busy) return;
    busy = true;
    banner = '';
    errors = {};
    try {
      const details = { label, exp_month: Number(month), exp_year: Number(year), name, billing };
      if (start) await updateWalletCard(start.id, details);
      else await addWalletCard({ ...details, number, cvc });
      clearSecrets();
      onSaved();
      onClose();
    } catch (e) {
      if (e instanceof AuthError) {
        clearSecrets();
        onError(e);
        onClose();
        return;
      }
      const error = e as Error & { field?: string };
      if (error.field) errors = { [error.field]: error.message };
      else banner = error.message || 'The card could not be saved.';
    } finally {
      busy = false;
    }
  }
</script>

<Modal
  bind:open
  title={start ? 'Edit card details' : 'Add card'}
  onOpenChange={(next) => {
    if (!next) close();
  }}
>
  <fieldset class="card-form" disabled={busy}>
    <Field label="Label" error={errors.label}><Input bind:value={label} autocomplete="off" /></Field
    >
    {#if !start}
      <Field label="Card number" error={errors.number}
        ><Input type="password" bind:value={number} inputmode="numeric" autocomplete="off" /></Field
      >
      <Field label="CVC" error={errors.cvc}
        ><Input type="password" bind:value={cvc} inputmode="numeric" autocomplete="off" /></Field
      >
    {/if}
    <Field label="Expiry month" error={errors.exp_month}
      ><Input bind:value={month} inputmode="numeric" /></Field
    >
    <Field label="Expiry year" error={errors.exp_year}
      ><Input bind:value={year} inputmode="numeric" /></Field
    >
    <Field label="Cardholder name" error={errors.name}
      ><Input bind:value={name} autocomplete="off" /></Field
    >
    {#each Object.entries(billingLabels) as [key, text]}
      <Field label={text}
        ><Input bind:value={billing[key as keyof WalletBilling]} autocomplete="off" /></Field
      >
    {/each}
    {#if errors.billing}<p class="banner error" role="alert">{errors.billing}</p>{/if}
    {#if banner}<p class="banner error" role="alert">{banner}</p>{/if}
  </fieldset>
  {#snippet footer()}
    <Button variant="ghost" onclick={close} disabled={busy}>Cancel</Button>
    <Button variant="primary" onclick={save} loading={busy}
      >{start ? 'Save details' : 'Add card'}</Button
    >
  {/snippet}
</Modal>

<style>
  .card-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    min-width: 0;
    margin: 0;
    padding: 0;
    border: 0;
  }
</style>
