<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import {
    KEY_RE,
    KEY_HINT,
    getLedgers,
    getAccounts,
    type EntityRow,
    type EntityInput,
  } from '$lib/money/api';
  import { Modal, Button, Select } from '$lib/components/ui';
  import { SettingsField } from '$lib/components/settings';

  /**
   * Create/edit form for a billing entity.
   *
   * Invoice identity and payment detection settings share this form. The
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
  let detectionEnabled = $state(untrack(() => entity?.payment_detection_enabled ?? false));
  let detectionLedger = $state(untrack(() => entity?.payment_detection_ledger ?? ''));
  let incomeAccount = $state(untrack(() => entity?.payment_detection_income_account ?? ''));
  let ledgers = $state<string[]>([]);
  let incomeAccounts = $state<string[]>([]);
  let ledgersLoading = $state(true);
  let accountsLoading = $state(false);
  let ledgerError = $state('');
  let accountError = $state('');
  let loadedLedger = $state('');
  let open = $state(true);

  onMount(() => {
    let active = true;
    async function loadLedgers() {
      try {
        const names = await getLedgers();
        if (!active) return;
        ledgers = names;
        // Older imported settings may use a different case than the registry.
        detectionLedger =
          names.find((name) => name.toLowerCase() === detectionLedger.toLowerCase()) ??
          detectionLedger;
      } catch (e) {
        if (active) ledgerError = e instanceof Error ? e.message : 'Failed to load ledgers';
      } finally {
        if (active) ledgersLoading = false;
      }
    }
    void loadLedgers();
    return () => {
      active = false;
    };
  });

  $effect(() => {
    const ledger = detectionLedger;
    let active = true;
    incomeAccounts = [];
    loadedLedger = '';
    accountError = '';
    accountsLoading = false;
    if (ledgersLoading || ledgerError || !ledgers.includes(ledger)) return;
    accountsLoading = true;
    async function loadAccounts() {
      try {
        const data = await getAccounts({ ledger });
        if (!active) return;
        incomeAccounts = data.accounts
          .map((row) => row.account)
          .filter((account) => account.startsWith('Income:'));
        loadedLedger = ledger;
        // Preserve a saved selection, including an unavailable one the user
        // must explicitly replace or disable.
        if (!incomeAccount && incomeAccounts.length === 1) incomeAccount = incomeAccounts[0];
      } catch (e) {
        if (active)
          accountError = e instanceof Error ? e.message : 'Failed to load income accounts';
      } finally {
        if (active) accountsLoading = false;
      }
    }
    void loadAccounts();
    return () => {
      active = false;
    };
  });

  function changeLedger(next: string) {
    if (next === detectionLedger) return;
    detectionLedger = next;
    incomeAccount = '';
  }

  const ledgerOptions = $derived([
    ...ledgers.map((name) => ({ value: name, label: name })),
    ...(detectionLedger && !ledgers.includes(detectionLedger)
      ? [{ value: detectionLedger, label: `${detectionLedger} (unavailable)`, disabled: true }]
      : []),
  ]);
  const incomeOptions = $derived([
    ...incomeAccounts.map((account) => ({ value: account, label: account })),
    ...(incomeAccount && !incomeAccounts.includes(incomeAccount)
      ? [{ value: incomeAccount, label: `${incomeAccount} (unavailable)`, disabled: true }]
      : []),
  ]);
  const ledgerProblem = $derived(
    ledgerError ||
      (ledgersLoading
        ? ''
        : !ledgers.length
          ? 'No ledgers configured.'
          : detectionLedger && !ledgers.includes(detectionLedger)
            ? 'Saved ledger is unavailable. Choose another ledger or turn detection off.'
            : ''),
  );
  const accountProblem = $derived(
    accountError ||
      (loadedLedger !== detectionLedger || !loadedLedger
        ? ''
        : !incomeAccounts.length
          ? 'No income accounts in this ledger.'
          : incomeAccount && !incomeAccounts.includes(incomeAccount)
            ? 'Saved income account is unavailable. Choose another account or turn detection off.'
            : ''),
  );
  const detectionReady = $derived(
    !ledgersLoading &&
      !accountsLoading &&
      !ledgerProblem &&
      !accountProblem &&
      !!detectionLedger &&
      loadedLedger === detectionLedger &&
      incomeAccounts.includes(incomeAccount),
  );

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
    !!name.trim() &&
      (isEdit || (!!key && !keyError)) &&
      !logoError &&
      !saving &&
      (!detectionEnabled || detectionReady),
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
      payment_detection_enabled: detectionEnabled,
      payment_detection_ledger: detectionLedger,
      payment_detection_income_account: incomeAccount,
    });
  }

  function handleOpenChange(next: boolean) {
    if (!next) onCancel();
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key !== 'Enter') return;
    // Only a single-line text input commits — Enter inside a textarea is a
    // newline, and inside any other control is that control's own business.
    if (!(e.target instanceof HTMLInputElement) || e.target.type === 'checkbox') return;
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

    <SettingsField label="Automatically detect invoice payments" checkbox>
      <input type="checkbox" bind:checked={detectionEnabled} disabled={saving} />
    </SettingsField>

    {#if detectionEnabled}
      <SettingsField label="Ledger" labelled={false} error={ledgerProblem}>
        <Select
          value={detectionLedger}
          onValueChange={changeLedger}
          options={ledgerOptions}
          placeholder="Choose a ledger"
          disabled={ledgersLoading || !!ledgerError || !ledgers.length || saving}
          fullWidth
          ariaLabel="Ledger"
        />
        {#if ledgersLoading}<span class="caption" role="status">Loading ledgers…</span>{/if}
      </SettingsField>
      <SettingsField
        label="Income account"
        labelled={false}
        error={accountProblem}
        hint="The revenue account invoice payments are booked to."
      >
        <Select
          bind:value={incomeAccount}
          options={incomeOptions}
          placeholder="Choose an income account"
          disabled={!detectionLedger ||
            loadedLedger !== detectionLedger ||
            accountsLoading ||
            !!accountError ||
            !incomeAccounts.length ||
            saving}
          fullWidth
          ariaLabel="Income account"
        />
        {#if accountsLoading}<span class="caption" role="status">Loading income accounts…</span
          >{/if}
      </SettingsField>
    {/if}
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

  /* Type is the global .form-error; only the space above it is this form's. */
  .form-error {
    margin-top: var(--space-2);
  }
</style>
