<script lang="ts">
  import { untrack } from 'svelte';
  import { KEY_RE, KEY_HINT, type EntityRow, type EntityInput } from '$lib/money/api';
  import { Modal, Button } from '$lib/components/ui';
  import { SettingsField } from '$lib/components/settings';

  /**
   * Create/edit form for a billing entity.
   *
   * Everything here lands on the generated invoice PDF, which is why the
   * delete guard behind it is strict: a client whose entity vanished falls
   * back to whichever company happens to be first, and the next invoice
   * carries a different legal entity's name, address and payment details.
   *
   * Optional fields clear with `""`, never `null` — the store skips null when
   * merging, so a null would leave the old value in place while the form
   * showed the field as cleared.
   */
  interface Props {
    entity?: EntityRow | null;
    onSave: (key: string, data: EntityInput) => void;
    onCancel: () => void;
    error?: string;
    saving?: boolean;
  }

  let { entity = null, onSave, onCancel, error = '', saving = false }: Props = $props();

  const isEdit = untrack(() => !!entity);

  let key = $state(untrack(() => entity?.key ?? ''));
  let name = $state(untrack(() => entity?.name ?? ''));
  let email = $state(untrack(() => entity?.email ?? ''));
  let address = $state(untrack(() => entity?.address ?? ''));
  let paymentInstructions = $state(untrack(() => entity?.payment_instructions ?? ''));
  let logo = $state(untrack(() => entity?.logo ?? ''));
  let arAccount = $state(untrack(() => entity?.ar_account ?? ''));
  let bankAccount = $state(untrack(() => entity?.bank_account ?? ''));
  let currency = $state(untrack(() => entity?.currency ?? ''));
  let open = $state(true);

  const keyError = $derived(!isEdit && key && !KEY_RE.test(key) ? KEY_HINT : '');
  // The logo is base64-embedded into the invoice, resolved against the
  // accounting folder — an absolute path or a `..` climb would reach outside
  // it. Rejected server-side too; this puts the message on the field.
  const logoError = $derived.by(() => {
    const value = logo.trim().replace(/\\/g, '/');
    if (!value) return '';
    const escapes =
      value.startsWith('/') || value.startsWith('~') || /^[A-Za-z]:/.test(value)
        ? true
        : value.split('/').includes('..');
    return escapes ? 'Expected a path inside the accounting folder' : '';
  });
  const canSave = $derived(
    !!name.trim() && (isEdit || (!!key && !keyError)) && !logoError && !saving,
  );

  function handleSave() {
    if (!canSave) return;
    onSave(isEdit ? (entity as EntityRow).key : key.trim(), {
      name: name.trim(),
      email: email.trim(),
      address,
      payment_instructions: paymentInstructions,
      logo: logo.trim(),
      ar_account: arAccount.trim(),
      bank_account: bankAccount.trim(),
      currency: currency.trim(),
    });
  }

  function handleOpenChange(next: boolean) {
    if (!next) onCancel();
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key !== 'Enter') return;
    // Only a single-line text input commits — Enter inside a textarea is a
    // newline, and inside any other control is that control's own business.
    if (!(e.target instanceof HTMLInputElement)) return;
    handleSave();
  }
</script>

<svelte:window on:keydown={handleKeydown} />

<Modal
  bind:open
  title={isEdit ? `Edit ${entity?.name || entity?.key}` : 'Add entity'}
  onOpenChange={handleOpenChange}
  width="420px"
>
  <div class="form-grid">
    {#if isEdit}
      <div class="static-key">
        <span>Key</span>
        <code>{entity?.key}</code>
        <small>The key is the identity — clients reference it by name.</small>
      </div>
    {:else}
      <SettingsField label="Key" hint="Short identifier clients point at." error={keyError}>
        <input type="text" bind:value={key} placeholder="main" autocomplete="off" />
      </SettingsField>
    {/if}

    <SettingsField label="Name" hint="Printed on the invoice.">
      <input type="text" bind:value={name} placeholder="Acme Studio LLC" />
    </SettingsField>

    <SettingsField label="Email">
      <input type="text" bind:value={email} placeholder="billing@example.com" />
    </SettingsField>

    <SettingsField label="Address" wide>
      <textarea rows="3" bind:value={address}></textarea>
    </SettingsField>

    <SettingsField label="Payment instructions" wide hint="Printed at the foot of the invoice.">
      <textarea rows="3" bind:value={paymentInstructions}></textarea>
    </SettingsField>

    <SettingsField
      label="Logo path"
      hint="Relative to your accounting folder, e.g. invoices/logo.png."
      error={logoError}
    >
      <input type="text" bind:value={logo} placeholder="invoices/logo.png" />
    </SettingsField>

    <SettingsField label="A/R account">
      <input type="text" bind:value={arAccount} placeholder="Assets:Accounts-Receivable" />
    </SettingsField>

    <SettingsField label="Bank account">
      <input type="text" bind:value={bankAccount} placeholder="Assets:Bank:Checking" />
    </SettingsField>

    <SettingsField label="Currency">
      <input type="text" bind:value={currency} placeholder="USD" />
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
    gap: var(--space-2);
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

  .form-error {
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
    margin-top: var(--space-2);
  }
</style>
