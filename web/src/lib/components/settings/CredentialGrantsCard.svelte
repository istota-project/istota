<script lang="ts">
  import { onMount } from 'svelte';
  import {
    AuthError,
    getCredentialGrants,
    saveCredentialGrant,
    revokeCredentialGrant,
    grantExistingCredentials,
    type CredentialGrant,
    type CredentialGrantsSettings,
  } from '$lib/api';
  import { Badge, Button, ConfirmDialog, KebabMenu, Modal, Select } from '$lib/components/ui';
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
  let methods: string[] = $state([]);
  let scheduled = $state(false);
  let omittedRooms = $state(false);
  let confirmExisting = $state(false);
  let confirmRevoke: string | null = $state(null);
  const defaultMethods = ['GET', 'HEAD', 'POST', 'PUT', 'PATCH'];
  const allMethods = [...defaultMethods, 'DELETE', 'OPTIONS'];

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
    methods = [...(grant?.methods ?? defaultMethods)];
    scheduled = grant?.allow_scheduled ?? false;
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
        methods,
        allow_scheduled: scheduled,
      }),
    );
  }
  function revoke() {
    const name = confirmRevoke;
    confirmRevoke = null;
    if (name) return mutate(() => revokeCredentialGrant(name));
  }

  function sourceLabel(c: Credential): string {
    return c.source === 'config' ? 'Deployment configuration' : 'Password vault';
  }

  /** What the grant allows, on one line: where, which methods, and whether a
   * scheduled task may use it. */
  function grantSummary(grant: CredentialGrant): string {
    const n = grant.rooms.length;
    const where = grant.scope_mode === 'all' ? 'All rooms' : `${n} room${n === 1 ? '' : 's'}`;
    const parts = [where, grant.methods.join(', ')];
    if (grant.allow_scheduled) parts.push('scheduled');
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
    return items;
  }
</script>

<SettingsCard title={data ? `Credentials (${data.credentials.length})` : 'Credentials'}>
  {#snippet actions()}
    {#if data?.grant_existing_available}
      <Button variant="pill" size="sm" onclick={() => (confirmExisting = true)} disabled={busy}>
        Grant what exists
      </Button>
    {/if}
  {/snippet}
  <p class="card-hint">
    Which rooms, HTTP methods and scheduled tasks may use each credential. A credential is sent only
    to the hosts it is bound to. Grants take effect when the credential broker is enabled.
  </p>
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
                  Set an HTTPS URL or <code>istota_hosts</code> in KeePassXC before using it.
                </span>
              {/if}
              <!-- Written without template whitespace so the line reads
                   exactly "Source · Rooms · Methods" with no stray gaps. -->
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
              {#if credential.revealable}<Badge variant="info">Revealable</Badge>{/if}
            </div>
            <KebabMenu items={menu(credential)} ariaLabel="Actions for {credential.name}" />
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</SettingsCard>

<Modal bind:open={editorOpen} title={`Grant for ${editing}`}>
  <div class="grant-fields">
    <label
      >Room scope
      <Select
        bind:value={scope}
        options={[
          { value: 'all', label: 'All rooms' },
          { value: 'rooms', label: 'Selected rooms' },
        ]}
      />
    </label>
    {#if omittedRooms}<p class="hint">
        Unavailable rooms have been removed from this selection.
      </p>{/if}
    {#if scope === 'rooms'}
      {#each data?.rooms ?? [] as room}
        <label><input type="checkbox" bind:group={rooms} value={room.token} /> {room.name}</label>
      {/each}
      {#if !data?.rooms.length}<p class="hint">
          No rooms available. This grant will allow no tasks.
        </p>{/if}
    {/if}
    <fieldset>
      <legend>Allowed HTTP methods</legend>
      {#each allMethods as method}
        <label><input type="checkbox" bind:group={methods} value={method} /> {method}</label>
      {/each}
    </fieldset>
    <label><input type="checkbox" bind:checked={scheduled} /> Allow scheduled tasks</label>
    {#if error}<p class="banner error" role="alert">{error}</p>{/if}
    <Button onclick={save} loading={busy} disabled={!methods.length}>Save grant</Button>
  </div>
</Modal>
<ConfirmDialog
  bind:open={confirmExisting}
  title="Grant current credentials"
  message="Allow all currently bound credentials in every room, including scheduled tasks? DELETE stays disabled. Credentials added later remain ungranted."
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
  .card-hint {
    margin: 0 0 var(--space-2);
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

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
  fieldset {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-3);
  }
</style>
