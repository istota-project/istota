<script lang="ts">
  import { onMount } from 'svelte';
  import {
    AuthError,
    getCredentialGrants,
    saveCredentialGrant,
    revokeCredentialGrant,
    grantExistingCredentials,
    type CredentialGrantsSettings,
  } from '$lib/api';
  import { Button, Select, Modal, ConfirmDialog } from '$lib/components/ui';
  import SettingsCard from './SettingsCard.svelte';

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
</script>

<SettingsCard title="Credentials">
  <p class="hint">
    Choose which rooms, HTTP methods and scheduled tasks may use each credential. These grants take
    effect when the credential broker is enabled.
  </p>
  {#if error}<p class="banner error" role="alert">{error}</p>{/if}
  {#if data}
    {#if !data.sandboxed}<p class="hint">
        Credential values are not contained on this deployment because tasks run without a
        filesystem sandbox.
      </p>{/if}
    {#if data.grant_existing_available}
      <Button onclick={() => (confirmExisting = true)} disabled={busy}>Grant what exists</Button>
    {/if}
    {#each data.credentials as credential (credential.name)}
      <div class="credential">
        <strong>{credential.name}</strong>
        <span class="hint"
          >{credential.source === 'config' ? 'Deployment configuration' : 'Password vault'}</span
        >
        {#if credential.revealable}<span class="status-pill">Revealable</span>{/if}
        {#if !credential.grant}<span class="status-pill">Ungranted</span>{/if}
        {#if credential.hosts.length}
          <p>{credential.hosts.join(', ')}</p>
          <p class="hint">Allowed headers: {credential.headers.join(', ') || 'None'}</p>
          <Button
            ariaLabel={`Edit grant for ${credential.name}`}
            onclick={() => edit(credential.name)}
            disabled={busy}>Edit grant</Button
          >
          {#if credential.grant}
            <Button
              variant="danger"
              onclick={() => mutate(() => revokeCredentialGrant(credential.name))}
              disabled={busy}>Revoke grant</Button
            >
          {/if}
        {:else}
          <p class="hint">
            Unbound. Set an HTTPS URL or istota_hosts in KeePassXC before using this credential.
          </p>
        {/if}
      </div>
    {:else}<p class="hint">No credentials have been stored.</p>{/each}
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

<style>
  .credential {
    border-top: 1px solid var(--border-default);
    padding: var(--space-4) 0;
    margin-top: var(--space-4);
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
