<script lang="ts">
  import { ConfirmDialog } from '$lib/components/ui';
  import { setSecret, deleteSecret, type ServiceCard as ServiceCardData } from '$lib/api';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';
  import SecretField from './SecretField.svelte';

  interface Props {
    service: ServiceCardData;
    onChanged?: () => void;
  }

  let { service, onChanged }: Props = $props();

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
  let confirmingClearKey: string | null = $state(null);

  let dirty = $derived(Object.values(pending).some((v) => v && v.length > 0));

  let clearMessage = $derived.by(() => {
    const field = service.fields.find((f) => f.key === confirmingClearKey);
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
    const entries = Object.entries(pending).filter(([, v]) => v && v.length > 0);
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
    confirmingClearKey = null;
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

  // The user's credential vault owns this service, so the fields here are not
  // where it is edited. The server refuses a write with a 409 either way — this
  // is what stops somebody typing a value into a form that was never going to
  // take it, which is the failure the disabled state exists to prevent rather
  // than the boundary.
  let vaultManaged = $derived(service.vault_managed === true);

  // The credential fields are part of the page's state, not a form of their
  // own, so they save from the app bar with everything else. A service with no
  // writable fields withdraws and leaves the button to the rest of the page,
  // and a vault-managed one has none it can write.
  // Clearing a stored secret stays an immediate, separately confirmed action:
  // it is a deletion, not an edit awaiting a save.
  useSettingsSave(() =>
    service.fields.length > 0 && !vaultManaged ? { dirty, saving, save: saveAll } : null,
  );
</script>

<section class="card" data-status={service.status}>
  <header class="section-header">
    <div class="title">
      <h2>{service.label}</h2>
      <span class="status-pill status-{service.status}">
        {statusLabel(service.status)}
      </span>
    </div>
    <div class="header-actions">
      {#if service.last_updated}
        <span class="meta">Updated {service.last_updated}</span>
      {/if}
      <!-- No Save button: the app bar owns it (see useSettingsSave above). The
			     flash stays, because with one shared button it is the only thing that
			     says *this* card is what got written. -->
      {#if savedFlash}
        <span class="saved-flash">Saved.</span>
      {/if}
    </div>
  </header>

  {#if service.hint}
    <p class="hint">{service.hint}</p>
  {/if}

  {#if service.used_by && service.used_by.length > 0}
    <p class="used-by">
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
    <p class="empty">No settings for this service.</p>
  {:else}
    {#if vaultManaged}
      <!--
        Above the fields rather than below them: it is the reason they are
        disabled, and a reader who meets a greyed-out input first has to hunt
        for the explanation. The referent — which file, where — is on the
        "Connected services" heading above every card, which is why this
        sentence can be short and why the vault is not a card of its own.
      -->
      <p class="hint vault-managed">
        These credentials come from your credential vault. Edit them in the vault file — a change
        here would be refused.
      </p>
    {/if}
    {#each service.fields as f (f.key)}
      <SecretField
        label={f.label}
        type={f.type}
        configured={service.configured_keys.includes(f.key)}
        value={pending[f.key] ?? ''}
        disabled={saving || vaultManaged}
        onValueChange={(v) => setFieldValue(f.key, v)}
        onRequestClear={vaultManaged ? undefined : () => (confirmingClearKey = f.key)}
      />
    {/each}
    {#if saveError}
      <div class="banner error">{saveError}</div>
    {/if}
  {/if}
</section>

<!--
  Clearing a stored credential is destructive and irreversible, so it goes
  through the shared dialog like every other such action — it used to be a
  hand-rolled inline row whose confirm button was `primary`, i.e. the same
  affordance as Save. One dialog outside the loop rather than one per field:
  `confirmingClearKey` already names the single field being cleared.
-->
<ConfirmDialog
  open={confirmingClearKey !== null}
  title="Clear stored value"
  message={clearMessage}
  confirmLabel="Clear"
  onConfirm={() => confirmingClearKey && performClear(confirmingClearKey)}
  onCancel={() => (confirmingClearKey = null)}
/>

<style>
  /* Inherit shared .settings .card / .section-header / .status-pill /
	   .meta / .empty styling — this component is meant to be used inside
	   <div class="settings"> wrappers that already pull in settings.css. */
  .used-by {
    margin: 0;
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .used-by code {
    background: var(--surface-raised);
    padding: 0 var(--space-1);
    border-radius: var(--radius-sm);
    font-size: 0.9em;
    color: var(--text-muted);
  }

  .saved-flash {
    font-size: var(--text-xs);
    color: var(--status-success-fg);
  }

  /* `.hint` carries the size and colour; this only keeps the sentence off the
     first field, which has no margin of its own above it. */
  .vault-managed {
    margin-bottom: var(--space-1);
  }
</style>
