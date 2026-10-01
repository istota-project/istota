<script lang="ts">
  import { onMount } from 'svelte';
  import {
    AuthError,
    getCredentialGrants,
    saveCredentialGrant,
    revokeCredentialGrant,
    deleteCredential,
    grantExistingCredentials,
    type CredentialGrant,
    type CredentialGrantsSettings,
  } from '$lib/api';
  import {
    Badge,
    Button,
    ConfirmDialog,
    Field,
    KebabMenu,
    Modal,
    Select,
  } from '$lib/components/ui';
  import type { KebabItem } from '$lib/components/ui/KebabMenu.svelte';
  import SettingsCard from './SettingsCard.svelte';

  type Credential = CredentialGrantsSettings['credentials'][number];

  let { onSignedOut = () => {} }: { onSignedOut?: () => void } = $props();
  let data: CredentialGrantsSettings | null = $state(null);
  let error = $state('');
  let busy = $state(false);
  let editing = $state('');
  let editorOpen = $state(false);
  let scope = $state<'all' | 'rooms'>('all');
  let rooms: string[] = $state([]);
  let scheduled = $state(false);
  let allowHttp = $state(false);
  let omittedRooms = $state(false);
  let confirmExisting = $state(false);
  let confirmRevoke: string | null = $state(null);
  let confirmDelete: string | null = $state(null);

  function report(e: unknown) {
    if (e instanceof AuthError) onSignedOut();
    else error = (e as Error).message || 'Credential grants could not be loaded.';
  }
  async function refresh() {
    try {
      data = await getCredentialGrants();
    } catch (e) {
      report(e);
    }
  }
  onMount(refresh);
  function edit(name: string) {
    const grant = data?.credentials.find((c) => c.name === name)?.grant;
    editing = name;
    scope = grant?.scope_mode ?? 'all';
    const availableRooms = new Set(data?.rooms.map((room) => room.token));
    rooms = (grant?.rooms ?? []).filter((room) => availableRooms.has(room));
    omittedRooms = rooms.length !== (grant?.rooms.length ?? 0);
    scheduled = grant?.allow_scheduled ?? false;
    allowHttp = grant?.allow_http ?? false;
    error = '';
    editorOpen = true;
  }
  async function mutate(action: () => Promise<unknown>) {
    if (busy) return;
    busy = true;
    error = '';
    try {
      await action();
      editorOpen = false;
      confirmExisting = false;
      await refresh();
    } catch (e) {
      report(e);
    } finally {
      busy = false;
    }
  }
  function save() {
    return mutate(() =>
      saveCredentialGrant(editing, {
        scope_mode: scope,
        rooms: scope === 'rooms' ? rooms : [],
        allow_scheduled: scheduled,
        allow_http: allowHttp,
      }),
    );
  }
  function revoke() {
    const name = confirmRevoke;
    confirmRevoke = null;
    if (name) return mutate(() => revokeCredentialGrant(name));
  }

  function remove() {
    const name = confirmDelete;
    confirmDelete = null;
    if (name) return mutate(() => deleteCredential(name));
  }

  function sourceLabel(c: Credential): string {
    return c.source === 'config' ? 'Deployment configuration' : 'Password vault';
  }

  /** Room scope, scheduled access and the HTTP override. */
  function grantSummary(grant: CredentialGrant): string {
    const n = grant.rooms.length;
    const where = grant.scope_mode === 'all' ? 'All rooms' : `${n} room${n === 1 ? '' : 's'}`;
    const parts = [where];
    if (grant.allow_scheduled) parts.push('scheduled');
    if (grant.allow_http) parts.push('HTTP allowed');
    return parts.join(' · ');
  }

  function menu(c: Credential): KebabItem[] {
    // Disabled rather than absent on an unbound credential: a grant needs a
    // host to bind to, and the row says how to give it one.
    const items: KebabItem[] = [
      { label: 'Edit grant', disabled: busy || !c.hosts.length, onSelect: () => edit(c.name) },
    ];
    if (c.grant)
      items.push({
        label: 'Revoke grant',
        danger: true,
        disabled: busy,
        onSelect: () => (confirmRevoke = c.name),
      });
    if (c.source === 'vault')
      items.push({
        label: 'Delete credential',
        danger: true,
        disabled: busy,
        onSelect: () => (confirmDelete = c.name),
      });
    return items;
  }
</script>

<SettingsCard
  title={data ? `Credentials (${data.credentials.length})` : 'Credentials'}
  description="Choose which rooms and scheduled tasks may use each credential. A credential is sent only to its bound domains, with all HTTP methods allowed. Grants take effect when the credential broker is enabled."
>
  {#snippet actions()}
    {#if data?.grant_existing_available}
      <Button variant="pill" size="sm" onclick={() => (confirmExisting = true)} disabled={busy}>
        Grant what exists
      </Button>
    {/if}
  {/snippet}
  {#if error && !editorOpen}<p class="banner error" role="alert">{error}</p>{/if}
  {#if data}
    {#if !data.sandboxed}
      <p class="banner info">
        Credential values are not contained on this deployment because tasks run without a
        filesystem sandbox.
      </p>
    {/if}
    {#if data.credentials.length === 0}
      <p class="empty">No credentials have been stored.</p>
    {:else}
      <ul class="cred-list">
        {#each data.credentials as credential (credential.name)}
          <li class="cred-row" data-testid="credential-{credential.name}">
            <code class="cred-name">{credential.name}</code>
            <div class="cred-main">
              {#if credential.hosts.length}
                <span class="cred-hosts">{credential.hosts.join(', ')}</span>
              {:else}
                <span class="cred-unbound">
                  Set a hostname, an HTTP or HTTPS URL, or <code>istota_hosts</code> in KeePassXC before
                  using it.
                </span>
              {/if}
              <!-- Written without template whitespace so the line reads
                   exactly "Source · Rooms" with no stray gaps. -->
              <span class="cred-meta"
                >{sourceLabel(credential)}{#if credential.grant}{' · ' +
                    grantSummary(credential.grant)}{/if}</span
              >
            </div>
            <div class="cred-badges">
              {#if !credential.hosts.length}
                <Badge variant="warn">Unbound</Badge>
              {:else if !credential.grant}
                <Badge variant="warn">Ungranted</Badge>
              {/if}
              {#if credential.hosts.some( (host) => host.startsWith('http://') ) && !credential.grant?.allow_http}
                <Badge variant="warn">HTTPS required</Badge>
              {/if}
              {#if credential.revealable}<Badge variant="info">Revealable</Badge>{/if}
            </div>
            <KebabMenu items={menu(credential)} ariaLabel="Actions for {credential.name}" />
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</SettingsCard>

<Modal bind:open={editorOpen} title="Edit grant">
  <div class="grant-fields">
    <code class="grant-name">{editing}</code>
    <Field label="Room scope" labelled={false}>
      <Select
        bind:value={scope}
        ariaLabel="Room scope"
        fullWidth
        options={[
          { value: 'all', label: 'All rooms' },
          { value: 'rooms', label: 'Selected rooms' },
        ]}
      />
    </Field>
    {#if omittedRooms}<p class="caption">
        Unavailable rooms have been removed from this selection.
      </p>{/if}
    {#if scope === 'rooms'}
      <fieldset class="grant-options">
        <legend>Rooms</legend>
        <div class="room-options">
          {#each data?.rooms ?? [] as room}
            <Field label={room.name} checkbox>
              <input type="checkbox" bind:group={rooms} value={room.token} />
            </Field>
          {/each}
        </div>
        {#if !data?.rooms.length}<p class="caption">
            No rooms available. This grant will allow no tasks.
          </p>{/if}
      </fieldset>
    {/if}
    <Field label="Allow scheduled tasks" checkbox>
      <input type="checkbox" bind:checked={scheduled} />
    </Field>
    <Field label="Allow HTTP (override HTTPS requirement)" checkbox>
      <input type="checkbox" bind:checked={allowHttp} />
    </Field>
    <p class="caption">
      HTTP sends credentials without encryption. Enable only for a service you trust on a trusted
      network.
    </p>
    {#if error}<p class="banner error" role="alert">{error}</p>{/if}
  </div>
  {#snippet footer()}
    <Button variant="ghost" onclick={() => (editorOpen = false)} disabled={busy}>Cancel</Button>
    <Button variant="primary" onclick={save} loading={busy}>Save grant</Button>
  {/snippet}
</Modal>
<ConfirmDialog
  open={confirmDelete !== null}
  title="Delete credential"
  message="Are you sure you want to delete {confirmDelete} and its grant? This removes the stored copy only. If it is still in KeePassXC, a later vault import can restore it without its grant."
  confirmLabel="Delete"
  confirmDisabled={busy}
  onConfirm={remove}
  onCancel={() => (confirmDelete = null)}
/>
<ConfirmDialog
  bind:open={confirmExisting}
  title="Grant current credentials"
  message="Allow all currently bound credentials in every room, including scheduled tasks? Credentials added later remain ungranted."
  confirmLabel="Grant what exists"
  onConfirm={() => mutate(grantExistingCredentials)}
  confirmDisabled={busy}
  confirmVariant="primary"
/>
<ConfirmDialog
  open={confirmRevoke !== null}
  title="Revoke grant"
  message="Are you sure you want to revoke the grant for {confirmRevoke}? No task can use it until it is granted again."
  confirmLabel="Revoke"
  confirmVariant="danger"
  onConfirm={revoke}
  onCancel={() => (confirmRevoke = null)}
/>

<style>
  .cred-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .cred-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2) 0;
  }

  .cred-row + .cred-row {
    border-top: 1px solid var(--border-subtle);
  }

  .cred-name {
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    color: var(--text-primary);
    background: var(--surface-raised);
    padding: 0 var(--space-1);
    border-radius: var(--radius-sm);
    flex: 0 1 9rem;
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .cred-main {
    display: flex;
    flex-direction: column;
    flex: 1 1 auto;
    min-width: 0;
  }

  .cred-hosts {
    font-size: var(--text-sm);
    color: var(--text-primary);
    overflow-wrap: anywhere;
  }

  .cred-unbound {
    font-size: var(--text-sm);
    color: var(--status-warn-fg);
  }

  .cred-meta {
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  .cred-badges {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: var(--space-1);
  }

  /* On a phone the name and the menu share the first line and the rest wraps
     under them, so a long host list is not squeezed into a sliver beside the
     badges. */
  @media (max-width: 600px) {
    .cred-row {
      flex-wrap: wrap;
    }
    .cred-name {
      flex: 1 1 auto;
    }
    .cred-row :global(.ui-kebab-trigger) {
      order: 1;
    }
    .cred-main {
      order: 2;
      flex-basis: 100%;
    }
    .cred-badges {
      order: 3;
      justify-content: flex-start;
    }
  }

  .grant-fields {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  .grant-name {
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }

  .grant-options {
    min-width: 0;
    margin: 0;
    padding: 0;
    border: 0;
  }

  .grant-options legend {
    padding: 0;
    margin-bottom: var(--space-2);
    font-size: var(--text-sm);
    color: var(--text-muted);
  }

  .room-options {
    display: grid;
    gap: var(--space-2);
    overflow-wrap: anywhere;
  }

  .grant-fields .caption {
    margin: 0;
  }
</style>
