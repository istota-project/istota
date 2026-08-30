<script lang="ts">
  import {
    getClients,
    getClientConfigs,
    getBusinessSettings,
    getWorkEntries,
    createClient,
    updateClient,
    deleteClient,
    ApiError,
    type ClientRow,
    type ClientConfigRow,
    type ClientInput,
    type EntityRow,
  } from '$lib/money/api';
  import { selectedLedger } from '$lib/money/stores/ledger';
  import {
    Button,
    ConfirmDialog,
    KebabMenu,
    NoticeBanner,
    type KebabItem,
  } from '$lib/components/ui';
  import ClientForm from '$lib/components/money/ClientForm.svelte';

  // Two reads of the same collection, each keeping one honest meaning:
  // `/clients` resolves the business defaults into `entity` and `ar_account`
  // for display, which is exactly wrong to bind an edit form to — saving it
  // back would *materialise* the default onto the record, so a later change
  // to `default_entity` would stop propagating to a client that never had an
  // explicit one. `/config/clients` is the raw shape the form edits.
  let clients: ClientRow[] = $state([]);
  let configs: Record<string, ClientConfigRow> = $state({});
  let entities: EntityRow[] = $state([]);
  let defaultEntity = $state('');
  let loading = $state(true);
  let error = $state('');
  let notice = $state('');

  let busyKey = $state('');

  let formOpen = $state(false);
  let editing: ClientConfigRow | null = $state(null);
  let formError = $state('');
  let saving = $state(false);

  let confirmOpen = $state(false);
  let pendingDelete: ClientRow | null = $state(null);
  let pendingReferences = $state(0);

  async function load() {
    loading = true;
    error = '';
    try {
      const [display, raw, settings] = await Promise.all([
        getClients(),
        getClientConfigs(),
        getBusinessSettings(),
      ]);
      clients = display.clients;
      configs = Object.fromEntries(raw.clients.map((c) => [c.key, c]));
      entities = settings.entities;
      defaultEntity = settings.defaults?.default_entity ?? '';
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load clients';
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    $selectedLedger;
    load();
  });

  function openAdd() {
    editing = null;
    formError = '';
    formOpen = true;
  }

  function openEdit(client: ClientRow) {
    editing = configs[client.key] ?? null;
    if (!editing) {
      notice = `Could not load the stored record for "${client.key}".`;
      return;
    }
    formError = '';
    formOpen = true;
  }

  function closeForm() {
    formOpen = false;
    editing = null;
    formError = '';
  }

  async function handleSave(key: string, data: ClientInput) {
    saving = true;
    formError = '';
    try {
      if (editing) {
        await updateClient(key, data);
      } else {
        await createClient(key, data);
      }
      closeForm();
      await load();
    } catch (e) {
      formError = e instanceof Error ? e.message : 'Failed to save client';
    } finally {
      saving = false;
    }
  }

  async function askDelete(client: ClientRow) {
    pendingDelete = client;
    pendingReferences = 0;
    confirmOpen = true;
    // Best-effort: the confirm still works without a count, it just can't
    // say what the deletion costs.
    try {
      const resp = await getWorkEntries({ client: client.key, status: 'all' });
      if (pendingDelete?.key === client.key) pendingReferences = resp.entries.length;
    } catch {
      /* leave the count out of the message */
    }
  }

  async function handleDelete() {
    const client = pendingDelete;
    confirmOpen = false;
    pendingDelete = null;
    if (!client) return;

    busyKey = client.key;
    try {
      await deleteClient(client.key);
      await load();
    } catch (e) {
      // A 409 here names records the user has to go look at, so it gets a
      // banner rather than being folded into the page-level error.
      if (e instanceof ApiError && e.status === 409) {
        notice = e.message;
      } else {
        error = e instanceof Error ? e.message : 'Failed to delete client';
      }
    } finally {
      busyKey = '';
    }
  }

  function menuItems(client: ClientRow): KebabItem[] {
    const busy = busyKey === client.key;
    return [
      { label: 'Edit', onSelect: () => openEdit(client), disabled: busy },
      { label: 'Delete', onSelect: () => askDelete(client), danger: true, disabled: busy },
    ];
  }

  const deleteMessage = $derived.by(() => {
    if (!pendingDelete) return '';
    const name = pendingDelete.name || pendingDelete.key;
    const head = `Are you sure you want to delete ${name}?`;
    if (!pendingReferences) return `${head} This cannot be undone.`;
    const plural = pendingReferences === 1 ? 'entry references' : 'entries reference';
    return (
      `${head} ${pendingReferences} work ${plural} this client and will show ` +
      `"${pendingDelete.key}" instead of a name.`
    );
  });
</script>

<div class="clients-content">
  {#if notice}
    <div class="money-notice-bar">
      <NoticeBanner title={notice} variant="warn" />
      <Button variant="ghost" onclick={() => (notice = '')}>Dismiss</Button>
    </div>
  {/if}

  <!-- Held back behind both whole-pane states, so the pane shows nothing but the
       centered message rather than centering it in the space left below. On
       error the count is also a lie: it reports 0 of something that failed to
       load. -->
  {#if !loading && !error}
    <div class="money-toolbar control-row">
      <span class="money-result-count">
        {clients.length}
        {clients.length === 1 ? 'client' : 'clients'}
      </span>
      <Button variant="primary" onclick={openAdd}>Add client</Button>
    </div>
  {/if}

  {#if loading}
    <div class="center-msg">Loading…</div>
  {:else if error}
    <div class="center-msg error">{error}</div>
  {:else if clients.length === 0}
    <div class="money-table-empty">No clients configured yet — add your first one.</div>
  {:else}
    <div class="client-grid card-grid">
      {#each clients as client (client.key)}
        <div class="client-card">
          <div class="card-header">
            <span class="client-name">{client.name}</span>
            <KebabMenu items={menuItems(client)} ariaLabel="Client actions" />
          </div>
          <div class="card-body">
            <span class="client-key">{client.key}</span>
            {#if client.email}
              <div class="card-field">
                <span class="field-label">Email</span>
                <span class="field-value">{client.email}</span>
              </div>
            {/if}
            {#if client.address}
              <div class="card-field">
                <span class="field-label">Address</span>
                <span class="field-value address">{client.address}</span>
              </div>
            {/if}
            <div class="card-field">
              <span class="field-label">Terms</span>
              <span class="field-value"
                >{typeof client.terms === 'number' ? `Net ${client.terms}` : client.terms}</span
              >
            </div>
            <div class="card-field">
              <span class="field-label">Entity</span>
              <span class="field-value">{client.entity_name || client.entity}</span>
            </div>
            {#if client.schedule !== 'on-demand'}
              <div class="card-field">
                <span class="field-label">Schedule</span>
                <span class="field-value">{client.schedule}, day {client.schedule_day}</span>
              </div>
            {/if}
            <div class="card-field">
              <span class="field-label">A/R account</span>
              <span class="field-value account">{client.ar_account}</span>
            </div>
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>

{#if formOpen}
  <ClientForm
    client={editing}
    {entities}
    {defaultEntity}
    onSave={handleSave}
    onCancel={closeForm}
    error={formError}
    {saving}
  />
{/if}

<ConfirmDialog
  bind:open={confirmOpen}
  title="Delete client"
  message={deleteMessage}
  confirmLabel="Delete"
  onConfirm={handleDelete}
  onCancel={() => (pendingDelete = null)}
/>

<style>
  /* No wrapper padding: the toolbar and grid carry the shared 0.75rem inline
     edge themselves, so this page's content lines up with the work and
     invoices tables on the sibling tabs. A growing column, matching the work
     and invoices tabs, so a whole-pane state (`.center-msg`) centers below the
     toolbar instead of hugging it. */
  .clients-content {
    display: flex;
    flex-direction: column;
    flex: 1 0 auto;
  }

  .client-grid {
    --card-min: 280px;
    padding: 0 var(--space-3) var(--space-3);
  }

  .client-card {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    overflow: hidden;
    transition: border-color var(--transition-fast);
  }

  .client-card:hover {
    border-color: var(--text-dim);
  }

  .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3);
    border-bottom: 1px solid var(--border-subtle);
  }

  .client-name {
    font-size: var(--text-base);
    font-weight: 600;
    color: var(--text-primary);
  }

  .client-key {
    font-size: var(--text-xs);
    color: var(--text-dim);
    font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, monospace;
  }

  .card-body {
    padding: var(--space-2) var(--space-3);
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .card-field {
    display: flex;
    gap: var(--space-2);
    font-size: var(--text-sm);
    line-height: 1.4;
  }

  .field-label {
    color: var(--text-dim);
    flex-shrink: 0;
    min-width: 5.5rem;
  }

  .field-value {
    color: var(--text-secondary);
    word-break: break-word;
  }

  .field-value.address {
    white-space: pre-line;
  }

  .field-value.account {
    font-size: var(--text-xs);
    font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, monospace;
  }

  @media (max-width: 640px) {
    .client-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
