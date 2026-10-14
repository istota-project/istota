<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    Button,
    Input,
    Field,
    Modal,
    ConfirmDialog,
    EmptyState,
    Badge,
  } from '$lib/components/ui';
  import { SettingsCard } from '$lib/components/settings';
  import GroupMembers from '$lib/components/groups/GroupMembers.svelte';
  import {
    getAdminGroups,
    getAdminGroup,
    getAdminGroupUserOptions,
    createAdminGroup,
    archiveAdminGroup,
    retryGroupStorage,
    AuthError,
    type AdminGroupSummary,
    type AdminGroupDetail,
  } from '$lib/api';
  import { getCurrentUser } from '$lib/userContext';
  import { createUrlSelection } from '$lib/navigation/urlSelection.svelte';

  const current = getCurrentUser();
  let groups = $state<AdminGroupSummary[]>([]);
  let group = $state<AdminGroupDetail | null>(null);
  let selected = $state<string | null>(null);
  let loading = $state(true);
  let detailLoading = $state(false);
  let error = $state('');
  let detailError = $state('');
  let busy = $state(false);
  let createOpen = $state(false);
  let archiveOpen = $state(false);
  let formError = $state('');
  let groupId = $state('');
  let displayName = $state('');
  let kind = $state('group');
  let owners = $state<string[]>([]);
  let users = $state<{ user_id: string; display_name: string }[]>([]);
  let storageNotes = $state<Record<string, string>>({});
  let storageErrors = $state<Record<string, string>>({});
  let active = true;
  let request = 0;
  const selection = createUrlSelection<string>({
    key: 'adminGroups',
    params: ['group'],
    encode: (id) => ({ group: id }),
    decode: (p) => p.group || null,
    read: () => selected,
    apply: select,
  });
  selection.start();

  function eventTitle(type: string): string {
    const titles: Record<string, string> = {
      created: 'Group created',
      updated: 'Details changed',
      member_added: 'Member added',
      member_removed: 'Member removed',
      role_changed: 'Role changed',
      archived: 'Group archived',
    };
    return titles[type] ?? 'Group change';
  }
  function eventDetails(event: AdminGroupDetail['events'][number]): string[] {
    const details = event.details;
    const lines: string[] = [];
    if (event.event_type === 'role_changed')
      lines.push(`Role: ${details.old_role} → ${details.new_role}`);
    if (event.event_type === 'member_added') {
      lines.push(`Role: ${details.role}`);
      if (details.acknowledge_history === true) lines.push('Existing history access acknowledged');
      const disclosure = details.disclosure as
        { memory?: string; rooms?: string[]; briefings?: string[] } | undefined;
      if (disclosure?.memory) lines.push(disclosure.memory);
      lines.push(...(disclosure?.rooms ?? []), ...(disclosure?.briefings ?? []));
    }
    if (event.event_type === 'member_removed') lines.push(`Previous role: ${details.old_role}`);
    if (Array.isArray(details.owners)) lines.push(`Initial owners: ${details.owners.join(', ')}`);
    for (const [field, label] of [
      ['display_name', 'Name'],
      ['kind', 'Kind'],
    ]) {
      const change = details[field] as { old: string; new: string } | undefined;
      if (change) lines.push(`${label}: ${change.old} → ${change.new}`);
    }
    return lines;
  }
  function message(e: unknown): string {
    if (e instanceof AuthError) current.expireSession();
    return e instanceof Error ? e.message : 'Could not complete this group operation.';
  }
  async function select(id: string) {
    selected = id;
    group = null;
    detailError = '';
    detailLoading = true;
    const pending = ++request;
    try {
      const result = await getAdminGroup(id);
      if (active && pending === request) group = result;
    } catch (e) {
      if (active && pending === request) detailError = message(e);
    } finally {
      if (active && pending === request) detailLoading = false;
    }
  }
  async function load() {
    loading = true;
    error = '';
    try {
      const result = await getAdminGroups();
      if (!active) return;
      groups = result.groups;
      const id = selection.current() ?? groups[0]?.group_id;
      if (id) await select(id);
    } catch (e) {
      if (active) error = message(e);
    } finally {
      if (active) loading = false;
    }
  }
  async function refresh() {
    if (!selected) return;
    const id = selected;
    const pending = ++request;
    const result = await getAdminGroup(id);
    const list = await getAdminGroups();
    if (active && pending === request) {
      group = result;
      groups = list.groups;
    }
  }
  async function openCreate() {
    groupId = displayName = formError = '';
    kind = 'group';
    owners = [];
    users = [];
    createOpen = true;
    busy = true;
    try {
      users = (await getAdminGroupUserOptions()).users;
    } catch (e) {
      formError = message(e);
    } finally {
      busy = false;
    }
  }
  async function create() {
    if (busy || !owners.length || !groupId.trim() || !displayName.trim()) return;
    busy = true;
    formError = '';
    try {
      const result = await createAdminGroup({
        revision: 0,
        group_id: groupId,
        display_name: displayName,
        kind,
        owners,
      });
      createOpen = false;
      if (!result.storage_available)
        storageNotes[result.group_id] =
          'Group created, but storage is unavailable. Retry setup after restoring the workspace.';
      // Creation is committed even if the following refresh fails.
      selection.push(result.group_id);
      try {
        groups = (await getAdminGroups()).groups;
      } catch (e) {
        detailError = message(e);
      }
    } catch (e) {
      formError = message(e);
    } finally {
      busy = false;
    }
  }
  async function archive() {
    if (busy || !group) return;
    busy = true;
    formError = '';
    try {
      await archiveAdminGroup(group.group_id, { revision: group.revision, confirm: true });
      archiveOpen = false;
      await refresh();
    } catch (e) {
      formError = message(e);
      if (!archiveOpen) detailError = formError;
    } finally {
      busy = false;
    }
  }
  async function reloadArchive() {
    archiveOpen = false;
    if (selected) await select(selected);
  }
  async function retryStorage() {
    if (busy || !group) return;
    const id = group.group_id;
    busy = true;
    storageErrors[id] = '';
    try {
      const result = await retryGroupStorage(id);
      storageNotes[id] = result.storage_available
        ? 'Group storage is ready.'
        : 'Group storage is unavailable. Retry setup after restoring the workspace.';
    } catch (e) {
      storageErrors[id] = message(e);
    } finally {
      busy = false;
    }
  }
  onMount(() => {
    load();
    return () => {
      active = false;
      ++request;
    };
  });
</script>

<div class="settings admin-page">
  {#if loading}<p class="center-msg">Loading groups…</p>
  {:else if error}<div class="center-msg error">
      <p>{error}</p>
      <Button onclick={load}>Retry</Button>
    </div>
  {:else}
    <SettingsCard
      title="Groups"
      description="Create groups and manage their owners and membership. Administration does not add you as a member."
    >
      {#snippet actions()}<Button variant="primary" onclick={openCreate} disabled={busy}
          >Create group</Button
        >{/snippet}
      {#if !groups.length}<EmptyState>No groups yet.</EmptyState>
      {:else}<div class="table-scroll">
          <table class="grid" aria-label="Groups">
            <thead
              ><tr><th scope="col">Name</th><th scope="col">Kind</th><th scope="col">Status</th></tr
              ></thead
            >
            <tbody
              >{#each groups as item (item.group_id)}<tr>
                  <td
                    ><Button
                      variant="ghost"
                      onclick={() => selection.push(item.group_id)}
                      disabled={busy}>{item.display_name}</Button
                    >
                    <div class="caption">{item.group_id}</div></td
                  >
                  <td>{item.kind}</td><td
                    >{#if item.archived_at}<Badge>Archived</Badge>{:else if item.needs_owner}<Badge
                        variant="warn">Needs owner</Badge
                      >{:else}Active{/if}</td
                  >
                </tr>{/each}</tbody
            >
          </table>
        </div>{/if}
    </SettingsCard>
    {#if detailLoading}<p class="center-msg">Loading group…</p>
    {:else if detailError}<div class="center-msg error">
        <p>{detailError}</p>
        <Button onclick={() => selected && select(selected)}>Reload group details</Button>
      </div>
    {:else if group}
      <SettingsCard title={group.display_name}>
        {#snippet actions()}
          {#if group && !group.archived_at && group.capabilities.archive}<Button
              variant="danger"
              disabled={busy}
              onclick={() => {
                formError = '';
                archiveOpen = true;
              }}>Archive group</Button
            >{/if}
        {/snippet}
        <dl class="kv">
          <dt>Group ID</dt>
          <dd>{group.group_id}</dd>
          <dt>Kind</dt>
          <dd>{group.kind}</dd>
          <dt>Created</dt>
          <dd>{group.created_at} by {group.created_by}</dd>
        </dl>
        {#if group.archived_at}<p>
            Archived {group.archived_at}. Data and membership history are preserved.
          </p>
        {:else if group.needs_owner}<p class="banner warn">Needs owner</p>{/if}
        {#if group.capabilities.read_memory}<Button
            href="{base}/groups/?group={encodeURIComponent(group.group_id)}"
            >Open group notebook</Button
          >{/if}
        {#key group.group_id}<GroupMembers {group} administration onChanged={refresh} />{/key}
      </SettingsCard>
      {#if !group.archived_at}
        <SettingsCard
          title="Storage"
          description="Retry setup if the group's workspace was unavailable. Existing files are kept."
        >
          {#if storageNotes[group.group_id]}<p role="status">{storageNotes[group.group_id]}</p>{/if}
          {#if storageErrors[group.group_id]}<p class="banner error" role="alert">
              {storageErrors[group.group_id]}
            </p>{/if}
          <Button onclick={retryStorage} loading={busy} loadingLabel="Working…"
            >Retry storage setup</Button
          >
        </SettingsCard>
      {/if}
      <SettingsCard title="Membership history">
        {#if !group.history.length}<EmptyState size="sm">No membership history.</EmptyState>{:else}
          <div class="table-scroll">
            <table class="grid" aria-label="Membership history">
              <thead
                ><tr
                  ><th scope="col">Member</th><th scope="col">Role</th><th scope="col">Joined</th
                  ><th scope="col">Left</th></tr
                ></thead
              >
              <tbody
                >{#each group.history as membership}<tr
                    ><td
                      >{membership.display_name}
                      <div class="caption">{membership.user_id}</div></td
                    ><td>{membership.role}</td><td
                      >{membership.added_at}
                      <div class="caption">Added by {membership.added_by}</div></td
                    ><td
                      >{membership.ended_at ?? 'Current member'}{#if membership.ended_by}<div
                          class="caption"
                        >
                          Removed by {membership.ended_by}
                        </div>{/if}</td
                    ></tr
                  >{/each}</tbody
              >
            </table>
          </div>
        {/if}
      </SettingsCard>
      <SettingsCard
        title="Management events"
        description="Role changes keep the membership period intact. The details below record each change."
      >
        {#if !group.events.length}<EmptyState size="sm">No management events.</EmptyState>{:else}<ul
            class="events"
          >
            {#each group.events as event (event.id)}<li>
                <strong>{eventTitle(event.event_type)}</strong> · {event.actor_id}{#if event.subject_user_id}
                  → {event.subject_user_id}{/if}
                <div class="caption">{event.created_at}</div>
                <ul>
                  {#each eventDetails(event) as line}<li>{line}</li>{/each}
                </ul>
              </li>{/each}
          </ul>{/if}
      </SettingsCard>
    {/if}
  {/if}
</div>

<Modal bind:open={createOpen} title="Create group">
  <div class="create-fields">
    <Field label="Group ID" warning="This identifier cannot be changed or reused after archiving."
      ><Input aria-label="Group ID" bind:value={groupId} disabled={busy} /></Field
    >
    <Field label="Name"><Input bind:value={displayName} disabled={busy} /></Field>
    <Field label="Kind" hint="A display label, such as family or team."
      ><Input bind:value={kind} disabled={busy} /></Field
    >
    <fieldset disabled={busy}>
      <legend>Initial owners</legend>
      <p>
        Choose at least one existing, enabled user. Owners can manage membership and read and edit
        the shared notebook.
      </p>
      {#each users as user (user.user_id)}<Field label={user.display_name} checkbox
          ><input type="checkbox" bind:group={owners} value={user.user_id} /></Field
        >{/each}
      {#if !busy && !users.length}<EmptyState size="sm">No enabled users are available.</EmptyState
        >{/if}
    </fieldset>
    {#if formError}<p class="banner error" role="alert">{formError}</p>{/if}
  </div>
  {#snippet footer()}<Button variant="ghost" onclick={() => (createOpen = false)} disabled={busy}
      >Cancel</Button
    ><Button
      variant="primary"
      onclick={create}
      loading={busy}
      disabled={!groupId.trim() || !displayName.trim() || !owners.length}>Create group</Button
    >{/snippet}
</Modal>
<ConfirmDialog
  bind:open={archiveOpen}
  title="Archive group"
  confirmLabel="Archive"
  confirmDisabled={busy}
  onConfirm={archive}
>
  {#snippet body()}<p>
      Are you sure you want to archive {group?.display_name}? Members will lose future access. Data
      and history are kept, and the group ID cannot be reused.
    </p>
    {#if formError}<p class="banner error" role="alert">{formError}</p>
      <Button onclick={reloadArchive} disabled={busy}>Reload group details</Button>{/if}
  {/snippet}
</ConfirmDialog>

<style>
  .create-fields {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  fieldset {
    border: 1px solid var(--border-default);
    padding: var(--space-3);
  }
  .events {
    padding-left: var(--space-4);
  }
  .events li {
    margin-block: var(--space-3);
  }
  .events {
    overflow-wrap: anywhere;
  }
</style>
