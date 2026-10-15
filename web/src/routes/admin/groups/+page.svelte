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
    NoticeBanner,
    PageLoading,
  } from '$lib/components/ui';
  import UserCell from '$lib/admin/UserCell.svelte';
  import { formatDate, formatDateTime, formatRelative } from '$lib/dateFormat';
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
  let aboutCollapsed = $state(true);
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

<div class="settings admin-page card-pane">
  {#if loading}<PageLoading label="Loading groups…" />
  {:else if error}<div class="center-msg error">
      <p>{error}</p>
      <Button onclick={load}>Retry</Button>
    </div>
  {:else}
    <NoticeBanner title="About group administration" bind:collapsed={aboutCollapsed}>
      <p>
        Create groups and manage their owners and membership. Administration does not add you as a
        member and does not open the group's notebook.
      </p>
      <p>
        Removing a member ends future access. Messages and downloads already received remain with
        their recipients. Archiving keeps the data and history, and the group ID cannot be reused.
      </p>
    </NoticeBanner>
    <SettingsCard title="Groups">
      {#snippet actions()}<Button variant="primary" size="sm" onclick={openCreate} disabled={busy}
          >Create group</Button
        >{/snippet}
      {#if !groups.length}<EmptyState
          >No groups yet. Create one to choose its first owners.</EmptyState
        >
      {:else}<div class="table-scroll">
          <table class="grid groups-grid" aria-label="Groups">
            <thead
              ><tr
                ><th scope="col">Group</th><th scope="col" class="col-kind">Kind</th><th
                  scope="col"
                  class="col-created">Created</th
                ><th scope="col" class="col-status">Status</th></tr
              ></thead
            >
            <tbody>
              {#each groups as item (item.group_id)}
                <!-- The row is a shortcut; the name button is the keyboard route. -->
                <tr
                  class="row-clickable"
                  class:selected={item.group_id === selected}
                  class:archived={item.archived_at}
                  onclick={() => !busy && selection.push(item.group_id)}
                >
                  <td>
                    <button
                      type="button"
                      class="group-name"
                      aria-current={item.group_id === selected ? 'true' : undefined}
                      disabled={busy}
                      onclick={(event) => {
                        event.stopPropagation();
                        selection.push(item.group_id);
                      }}>{item.display_name}</button
                    >
                    <div class="caption mono">{item.group_id}</div>
                  </td>
                  <td class="col-kind muted">{item.kind}</td>
                  <td class="col-created muted" title={formatDateTime(item.created_at)}
                    >{formatDate(item.created_at)}</td
                  >
                  <td class="col-status"
                    >{#if item.archived_at}<Badge size="sm">Archived</Badge
                      >{:else if item.needs_owner}<Badge size="sm" variant="warn">Needs owner</Badge
                      >{:else}<Badge size="sm" variant="success">Active</Badge>{/if}</td
                  >
                </tr>
              {/each}
            </tbody>
          </table>
        </div>{/if}
    </SettingsCard>
    {#if detailLoading}<PageLoading label="Loading group…" />
    {:else if detailError}<div class="center-msg error">
        <p>{detailError}</p>
        <Button onclick={() => selected && select(selected)}>Reload group details</Button>
      </div>
    {:else if group}
      <SettingsCard title={group.display_name}>
        {#snippet status()}
          {#if group?.archived_at}<Badge size="sm">Archived</Badge
            >{:else if group?.needs_owner}<Badge size="sm" variant="warn">Needs owner</Badge>{/if}
        {/snippet}
        {#snippet actions()}
          {#if group?.capabilities.read_memory}<Button
              size="sm"
              href="{base}/groups/?group={encodeURIComponent(group.group_id)}"
              >Open group notebook</Button
            >{/if}
          {#if group && !group.archived_at && group.capabilities.archive}<Button
              variant="danger"
              size="sm"
              disabled={busy}
              onclick={() => {
                formError = '';
                archiveOpen = true;
              }}>Archive group</Button
            >{/if}
        {/snippet}
        <dl class="kv group-facts">
          <dt>Group ID</dt>
          <dd class="mono">{group.group_id}</dd>
          <dt>Kind</dt>
          <dd>{group.kind}</dd>
          <dt>Created</dt>
          <dd>
            <span title={formatDateTime(group.created_at)}>{formatDate(group.created_at)}</span>
            <span class="muted">by {group.created_by}</span>
          </dd>
          {#if group.archived_at}
            <dt>Archived</dt>
            <dd>
              <span title={formatDateTime(group.archived_at)}>{formatDate(group.archived_at)}</span>
              <span class="muted">· data and membership history are kept</span>
            </dd>
          {/if}
        </dl>
        {#if !group.archived_at && group.needs_owner}<p class="banner warn">
            This group has no owner. Members can still use it, but nobody can manage its membership
            until you assign one.
          </p>{/if}
      </SettingsCard>
      {#if !group.archived_at}
        <SettingsCard title="Members">
          {#key group.group_id}<GroupMembers {group} administration onChanged={refresh} />{/key}
        </SettingsCard>
        <SettingsCard
          title="Storage"
          description="Retry setup if the group's workspace was unavailable. Existing files are kept."
        >
          {#snippet actions()}<Button
              size="sm"
              onclick={retryStorage}
              loading={busy}
              loadingLabel="Working…">Retry storage setup</Button
            >{/snippet}
          {#if storageNotes[group.group_id]}<p class="banner info" role="status">
              {storageNotes[group.group_id]}
            </p>{/if}
          {#if storageErrors[group.group_id]}<p class="banner error" role="alert">
              {storageErrors[group.group_id]}
            </p>{/if}
        </SettingsCard>
      {/if}
      <SettingsCard title="Membership history">
        {#if !group.history.length}<EmptyState size="sm">No membership history.</EmptyState>{:else}
          <div class="table-scroll">
            <table class="grid" aria-label="Membership history">
              <thead
                ><tr
                  ><th scope="col">Member</th><th scope="col" class="col-role">Role</th><th
                    scope="col">Joined</th
                  ><th scope="col">Left</th></tr
                ></thead
              >
              <tbody
                >{#each group.history as membership}<tr
                    ><td
                      ><UserCell
                        userId={membership.user_id}
                        displayName={membership.display_name}
                      /></td
                    ><td class="col-role"
                      ><Badge size="sm" variant={membership.role === 'owner' ? 'info' : 'neutral'}
                        >{membership.role === 'owner' ? 'Owner' : 'Member'}</Badge
                      ></td
                    ><td
                      ><span title={formatDateTime(membership.added_at)}
                        >{formatDate(membership.added_at)}</span
                      >
                      <div class="caption">by {membership.added_by}</div></td
                    ><td
                      >{#if membership.ended_at}<span title={formatDateTime(membership.ended_at)}
                          >{formatDate(membership.ended_at)}</span
                        >{#if membership.ended_by}<div class="caption">
                            by {membership.ended_by}
                          </div>{/if}{:else}<span class="muted">Current member</span>{/if}</td
                    ></tr
                  >{/each}</tbody
              >
            </table>
          </div>
        {/if}
      </SettingsCard>
      <SettingsCard
        title="Management events"
        description="Role changes keep the membership period intact. Each change is recorded here."
      >
        {#if !group.events.length}<EmptyState size="sm">No management events.</EmptyState>{:else}<ol
            class="events"
          >
            {#each group.events as event (event.id)}
              {@const lines = eventDetails(event)}
              <li class="event">
                <div class="event-head">
                  <span class="event-title">{eventTitle(event.event_type)}</span>
                  <span class="caption" title={formatDateTime(event.created_at)}
                    >{formatRelative(event.created_at)}</span
                  >
                </div>
                <div class="caption">
                  by {event.actor_id}{#if event.subject_user_id}
                    · {event.subject_user_id}{/if}
                </div>
                {#if lines.length}<ul class="event-details">
                    {#each lines as line}<li>{line}</li>{/each}
                  </ul>{/if}
              </li>
            {/each}
          </ol>{/if}
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
  {#snippet footer()}<Button
      variant="secondary"
      onclick={() => (createOpen = false)}
      disabled={busy}>Cancel</Button
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
  .create-fields p {
    margin: 0;
  }
  fieldset {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    margin: 0;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    padding: var(--space-3);
  }
  legend {
    padding-inline: var(--space-1);
    font-weight: 500;
  }
  .row-clickable {
    cursor: pointer;
  }
  .row-clickable:hover,
  .row-clickable.selected {
    background: var(--surface-raised);
  }
  .row-clickable.selected td:first-child {
    box-shadow: inset 2px 0 0 var(--accent);
  }
  .row-clickable.archived .group-name {
    color: var(--text-muted);
  }
  .group-name {
    padding: 0;
    border: none;
    background: none;
    color: var(--text-primary);
    font: inherit;
    font-weight: 500;
    text-align: left;
    cursor: pointer;
    overflow-wrap: anywhere;
  }
  .mono {
    font-family: var(--font-mono);
  }
  .col-kind,
  .col-role {
    width: 7rem;
  }
  .col-created,
  .col-status {
    width: 8rem;
  }
  .group-facts {
    --kv-gap: var(--space-6);
    margin: 0;
  }
  .group-facts dd {
    overflow-wrap: anywhere;
  }
  .group-facts + .banner {
    margin-top: var(--space-3);
  }
  .events {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .event {
    padding-block: var(--space-2);
    border-bottom: 1px solid var(--border-subtle);
    overflow-wrap: anywhere;
  }
  .event:last-child {
    border-bottom: none;
  }
  .event-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: var(--space-3);
  }
  .event-title {
    font-weight: 500;
  }
  .event-details {
    margin: var(--space-1) 0 0;
    padding-left: var(--space-4);
    color: var(--text-muted);
    font-size: var(--text-sm);
  }
  @container settings (max-width: 40rem) {
    .col-created,
    .col-kind {
      display: none;
    }
  }
</style>
