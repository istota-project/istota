<script lang="ts">
  import { createConfirm } from '@minot/ui/ui/confirm.svelte';
  import {
    Alert,
    Badge,
    Button,
    Card,
    ConfirmDialog,
    EmptyState,
    Field,
    Input,
    SecretField,
    type BadgeVariant,
  } from '@minot/ui';
  import { setSecret, deleteSecret, type ServiceCard as ServiceCardData } from '$lib/api';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';

  interface Props {
    service: ServiceCardData;
    onChanged?: () => void;
  }

  let { service, onChanged }: Props = $props();

  const STATUS_VARIANT: Record<ServiceCardData['status'], BadgeVariant> = {
    configured: 'success',
    partial: 'warn',
    missing: 'neutral',
    unavailable: 'neutral',
  };

  function statusLabel(s: ServiceCardData['status']): string {
    switch (s) {
      case 'configured':
        return 'Configured';
      case 'partial':
        return 'Partial';
      case 'missing':
        return 'Missing';
      case 'unavailable':
        return 'Not enabled';
    }
  }

  let pending: Record<string, string> = $state({});
  let saving = $state(false);
  let savedFlash = $state(false);
  let saveError = $state('');
  const confirmingClearKey = createConfirm<string>();

  // A plain field (a setting, not a secret) comes back with its stored value
  // and is shown prefilled; retyping that same value is not a change.
  function changedEntries(): [string, string][] {
    return Object.entries(pending).filter(
      ([key, v]) => v && v.length > 0 && v !== service.values?.[key],
    );
  }

  let dirty = $derived(changedEntries().length > 0);

  let clearMessage = $derived.by(() => {
    const field = service.fields.find((f) => f.key === confirmingClearKey.target);
    return (
      `Are you sure you want to clear the stored ${field?.label ?? 'value'} ` +
      `for ${service.label}? The credential is deleted from the server and ` +
      'cannot be recovered — you would have to enter it again.'
    );
  });

  function setFieldValue(key: string, next: string) {
    pending = { ...pending, [key]: next };
  }

  async function saveAll() {
    const entries = changedEntries();
    if (entries.length === 0) return;
    saving = true;
    saveError = '';
    try {
      for (const [key, value] of entries) {
        await setSecret(service.service, key, value);
      }
      pending = {};
      savedFlash = true;
      setTimeout(() => {
        savedFlash = false;
      }, 1500);
      onChanged?.();
    } catch (e) {
      saveError = e instanceof Error ? e.message : 'Save failed';
    } finally {
      saving = false;
    }
  }

  async function performClear(key: string) {
    confirmingClearKey.cancel();
    saving = true;
    saveError = '';
    try {
      await deleteSecret(service.service, key);
      // Drop any pending edit for the cleared key.
      if (key in pending) {
        const { [key]: _drop, ...rest } = pending;
        pending = rest;
      }
      onChanged?.();
    } catch (e) {
      saveError = e instanceof Error ? e.message : 'Delete failed';
    } finally {
      saving = false;
    }
  }

  // The credential fields are part of the page's state, not a form of their
  // own, so they save from the app bar with everything else. A service with no
  // writable fields withdraws and leaves the button to the rest of the page.
  // Clearing a stored secret stays an immediate, separately confirmed action:
  // it is a deletion, not an edit awaiting a save.
  useSettingsSave(() => (service.fields.length > 0 ? { dirty, saving, save: saveAll } : null));
</script>

<Card title={service.label} description={service.hint || undefined} data-status={service.status}>
  {#snippet status()}
    <Badge tone="subtle" uppercase={false} variant={STATUS_VARIANT[service.status]}>
      {statusLabel(service.status)}
    </Badge>
  {/snippet}
  {#snippet actions()}
    {#if service.last_updated}
      <span class="caption">Updated {service.last_updated}</span>
    {/if}
    <!-- No Save button: the app bar owns it (see useSettingsSave above). The
		     flash stays, because with one shared button it is the only thing that
		     says *this* card is what got written. -->
    {#if savedFlash}
      <span class="saved-flash">Saved.</span>
    {/if}
  {/snippet}

  {#if service.used_by && service.used_by.length > 0}
    <p class="used-by caption">
      Used by:
      {#each service.used_by as skill, i (skill)}
        {#if i > 0},{' '}{/if}
        <code>{skill}</code>
      {/each}
    </p>
  {/if}

  <!--
    This card is the writable-fields shape. A service whose auth is a flow
    rather than a set of fields declares `custom_ui` and gets its own component
    (GarminCard, GoogleWorkspaceCard) — the generic Connect/Disconnect branch
    that used to live here served exactly one service, which outgrew it.
  -->
  {#if service.fields.length === 0}
    <EmptyState>No settings for this service.</EmptyState>
  {:else}
    <!--
      There is no read-only branch here any more. A credential vault used to be
      able to own a typed service and overwrite its fields, so a card for one
      rendered disabled with a sentence saying where to edit it instead. A vault
      writes only its own namespace of shared credentials now, nothing
      overwrites what is typed here, and the 409 that refused such a write went
      with the ownership it enforced.
    -->
    {#each service.fields as f (f.key)}
      {#if f.plain}
        {@const configured = service.configured_keys.includes(f.key)}
        <Field label={f.label}>
          <div class="plain-row hstack">
            <Input
              type={f.type}
              value={pending[f.key] ?? service.values?.[f.key] ?? ''}
              disabled={saving}
              placeholder="Enter value"
              aria-label={configured ? f.label : undefined}
              oninput={(e: Event) =>
                setFieldValue(f.key, (e.currentTarget as HTMLInputElement).value)}
            />
            {#if configured}
              <Button
                variant="secondary"
                size="sm"
                class="plain-clear-btn"
                ariaLabel="Clear stored {f.label}"
                title="Clear stored value"
                disabled={saving}
                onclick={() => confirmingClearKey.ask(f.key)}>Clear</Button
              >
            {/if}
          </div>
        </Field>
      {:else}
        <SecretField
          label={f.label}
          type={f.type}
          configured={service.configured_keys.includes(f.key)}
          value={pending[f.key] ?? ''}
          disabled={saving}
          onValueChange={(v) => setFieldValue(f.key, v)}
          onRequestClear={() => confirmingClearKey.ask(f.key)}
        />
      {/if}
    {/each}
    {#if saveError}
      <Alert appearance="inline" variant="danger" role="alert">{saveError}</Alert>
    {/if}
  {/if}
</Card>

<!--
  Clearing a stored credential is destructive and irreversible, so it goes
  through the shared dialog like every other such action — it used to be a
  hand-rolled inline row whose confirm button was `primary`, i.e. the same
  affordance as Save. One dialog outside the loop rather than one per field:
  `confirmingClearKey.target` already names the single field being cleared.
-->
<ConfirmDialog
  bind:open={confirmingClearKey.open}
  title="Clear stored value"
  message={clearMessage}
  confirmLabel="Clear"
  onConfirm={() => confirmingClearKey.target && performClear(confirmingClearKey.target)}
/>

<style>
  .used-by {
    margin: 0;
  }

  .used-by code {
    background: var(--surface-raised);
    padding: 0 var(--space-1);
    border-radius: var(--radius-sm);
    color: var(--text-muted);
  }

  /* Same widths as SecretField's row, so a plain field lines up with the
     credential fields beside it. */
  .plain-row > :global(.ui-input) {
    flex: 0 1 24rem;
  }

  .plain-row > :global(.plain-clear-btn) {
    flex: none;
  }

  .saved-flash {
    font-size: var(--text-xs);
    color: var(--status-success-fg);
  }
</style>
