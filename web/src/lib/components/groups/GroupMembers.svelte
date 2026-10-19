<script lang="ts">
  import {
    Button,
    Field,
    Input,
    Select,
    Modal,
    ConfirmDialog,
    KebabMenu,
    Badge,
  } from '$lib/components/ui';
  import UserCell from '$lib/admin/UserCell.svelte';
  import {
    getGroupUserOptions,
    getAdminGroupUserOptions,
    updateAdminGroup,
    putGroupMember,
    deleteGroupMember,
    updateGroup,
    GroupApiError,
    AuthError,
    type GroupDetail,
    type AdminGroupDetail,
    type GroupMember,
    type GroupDisclosure,
  } from '$lib/api';
  import { getCurrentUser } from '$lib/userContext';
  let {
    group,
    onChanged,
    administration = false,
  }: {
    group: GroupDetail | AdminGroupDetail;
    onChanged: (left?: boolean) => Promise<void>;
    administration?: boolean;
  } = $props();
  const current = getCurrentUser();
  const user = $derived(current.user);
  let busy = $state(false);
  let error = $state('');
  let memberOpen = $state(false);
  let metadataOpen = $state(false);
  let removeOpen = $state(false);
  let target = $state<GroupMember | null>(null);
  let userId = $state('');
  let role = $state<'member' | 'owner'>('member');
  let replacement = $state('');
  let acknowledged = $state(false);
  let users = $state<{ user_id: string; display_name: string }[]>([]);
  let disclosure = $state<GroupDisclosure | null>(null);
  let displayName = $state('');
  let kind = $state('');
  const replacements = $derived([
    { value: '', label: 'No replacement' },
    ...group.members
      .filter((m) => m.user_id !== userId)
      .map((m) => ({ value: m.user_id, label: m.display_name })),
  ]);
  const options = $derived(
    users
      .filter((u) => !group.members.some((m) => m.user_id === u.user_id))
      .map((u) => ({ value: u.user_id, label: u.display_name })),
  );

  function report(e: unknown) {
    if (e instanceof AuthError) current.expireSession();
    error = e instanceof Error ? e.message : 'Could not save this change.';
    if (e instanceof GroupApiError && e.disclosure) disclosure = e.disclosure;
  }
  async function openMember(member: GroupMember | null, initialRole?: 'member' | 'owner') {
    target = member;
    userId = member?.user_id ?? '';
    role = initialRole ?? member?.role ?? 'member';
    replacement = '';
    acknowledged = false;
    disclosure = group.disclosure;
    error = '';
    memberOpen = true;
    if (!member) {
      busy = true;
      users = [];
      try {
        users = (
          await (administration ? getAdminGroupUserOptions() : getGroupUserOptions(group.group_id))
        ).users;
        userId = options[0]?.value ?? '';
      } catch (e) {
        report(e);
      } finally {
        busy = false;
      }
    }
  }
  async function saveMember() {
    if (busy || !userId || (!target && !acknowledged)) return;
    busy = true;
    error = '';
    try {
      const payload = {
        revision: group.revision,
        role,
        ...(!target ? { acknowledge_history: acknowledged } : {}),
        ...(replacement ? { replacement_owner: replacement } : {}),
      };
      if (administration) await updateAdminGroup(group.group_id, { ...payload, user_id: userId });
      else await putGroupMember(group.group_id, userId, payload);
      memberOpen = false;
      await onChanged();
    } catch (e) {
      report(e);
    } finally {
      busy = false;
    }
  }
  function openRemove(member: GroupMember) {
    target = member;
    userId = member.user_id;
    replacement = '';
    error = '';
    removeOpen = true;
  }
  async function removeMember() {
    if (busy) return;
    busy = true;
    error = '';
    try {
      const payload = {
        revision: group.revision,
        ...(replacement ? { replacement_owner: replacement } : {}),
      };
      if (administration)
        await updateAdminGroup(group.group_id, { ...payload, user_id: userId, role: null });
      else await deleteGroupMember(group.group_id, userId, payload);
      removeOpen = false;
      await onChanged(!administration && userId === user.username);
    } catch (e) {
      report(e);
    } finally {
      busy = false;
    }
  }
  function openMetadata() {
    displayName = group.display_name;
    kind = group.kind;
    error = '';
    metadataOpen = true;
  }
  async function saveMetadata() {
    if (busy) return;
    busy = true;
    error = '';
    try {
      await (administration ? updateAdminGroup : updateGroup)(group.group_id, {
        revision: group.revision,
        display_name: displayName,
        kind,
      });
      metadataOpen = false;
      await onChanged();
    } catch (e) {
      report(e);
    } finally {
      busy = false;
    }
  }
</script>

<div class="vstack">
  {#if group.system_key === 'all_users'}
    <p class="caption">Everyone in this installation; membership is automatic.</p>
  {/if}
  <div class="actions">
    {#if group.capabilities.manage && !group.system_key}
      <Button size="sm" onclick={() => openMember(null)}>Add member</Button>
      {#if administration && 'needs_owner' in group && group.needs_owner}
        <Button size="sm" variant="primary" onclick={() => openMember(null, 'owner')}
          >Assign owner</Button
        >
      {/if}
      <Button size="sm" onclick={openMetadata}>Edit group details</Button>
    {/if}
    {#if !administration && group.capabilities.leave && !group.system_key}
      <Button
        variant="danger"
        size="sm"
        onclick={() => {
          const self = group.members.find((m) => m.user_id === user.username);
          if (self) openRemove(self);
        }}>Leave group</Button
      >
    {/if}
  </div>
  <ul class="member-list list-reset">
    {#each group.members as member (member.user_id)}
      <li>
        <span class="member-name"
          ><UserCell userId={member.user_id} displayName={member.display_name} /></span
        >
        <Badge variant={member.role === 'owner' ? 'info' : 'neutral'} size="sm"
          >{member.role === 'owner' ? 'Owner' : 'Member'}</Badge
        >
        {#if group.capabilities.manage && !group.system_key}<KebabMenu
            ariaLabel={`Actions for ${member.display_name}`}
            items={[
              { label: 'Change role', onSelect: () => openMember(member) },
              ...(administration && member.role !== 'owner'
                ? [{ label: 'Make owner', onSelect: () => openMember(member, 'owner') }]
                : []),
              { label: 'Remove member', danger: true, onSelect: () => openRemove(member) },
            ]}
          />{/if}
      </li>
    {/each}
  </ul>
  <p class="caption">
    Removal ends future access. Messages and downloads already received remain with their
    recipients.
  </p>
</div>

<Modal bind:open={memberOpen} title={target ? 'Change member role' : 'Add member'}>
  <div class="form-fields vstack">
    {#if target}<p>{target.display_name}</p>{:else}
      <Field label="User"
        ><Select bind:value={userId} {options} ariaLabel="User" fullWidth disabled={busy} /></Field
      >
      {#if !busy && !options.length}<p class="caption">
          Every available user is already a member.
        </p>{/if}
    {/if}
    <Field label="Role"
      ><Select
        bind:value={role}
        options={[
          { value: 'member', label: 'Member' },
          { value: 'owner', label: 'Owner' },
        ]}
        ariaLabel="Role"
        fullWidth
      /></Field
    >
    {#if target}
      <Field
        label="Replacement owner"
        warning="If this would leave other members without an owner, choose a replacement in the same change."
        ><Select
          bind:value={replacement}
          options={replacements}
          ariaLabel="Replacement owner"
          fullWidth
        /></Field
      >
    {:else if disclosure}
      <div class="disclosure">
        <p>This person will be able to read the existing shared material:</p>
        <ul>
          <li>{disclosure.memory}</li>
          {#each disclosure.rooms as room}<li>{room}</li>{/each}
          {#each disclosure.briefings as briefing}<li>{briefing}</li>{/each}
        </ul>
      </div>
      <Field label="I acknowledge access to this existing history" checkbox
        ><input type="checkbox" bind:checked={acknowledged} /></Field
      >
    {/if}
    {#if error}<p class="banner error">{error}</p>
      <Button onclick={() => onChanged()}>Reload group details</Button>{/if}
  </div>
  {#snippet footer()}<Button
      variant="secondary"
      onclick={() => (memberOpen = false)}
      disabled={busy}>Cancel</Button
    ><Button
      variant="primary"
      onclick={saveMember}
      loading={busy}
      disabled={!userId || (!target && !acknowledged)}>{target ? 'Save role' : 'Add member'}</Button
    >{/snippet}
</Modal>
<Modal bind:open={metadataOpen} title="Edit group details">
  <div class="form-fields vstack">
    <Field label="Name"><Input bind:value={displayName} /></Field>
    <Field label="Kind" hint="A display label, such as family or team."
      ><Input bind:value={kind} /></Field
    >
    <p class="caption">Group ID: {group.group_id}</p>
    {#if error}<p class="banner error">{error}</p>
      <Button onclick={() => onChanged()}>Reload group details</Button>{/if}
  </div>
  {#snippet footer()}<Button
      variant="secondary"
      onclick={() => (metadataOpen = false)}
      disabled={busy}>Cancel</Button
    ><Button variant="primary" onclick={saveMetadata} loading={busy} disabled={!displayName.trim()}
      >Save details</Button
    >{/snippet}
</Modal>
<ConfirmDialog
  bind:open={removeOpen}
  title={!administration && userId === user.username ? 'Leave group' : 'Remove member'}
  confirmLabel={!administration && userId === user.username ? 'Leave' : 'Remove'}
  confirmDisabled={busy}
  onConfirm={removeMember}
>
  {#snippet body()}
    <p>
      Are you sure you want to {!administration && userId === user.username
        ? 'leave this group'
        : `remove ${target?.display_name}`}? Future access ends. Already delivered messages and
      downloads remain with recipients.
    </p>
    <Field
      label="Replacement owner"
      warning="Choose a replacement if this would leave other members without an owner."
      ><Select
        bind:value={replacement}
        options={replacements}
        ariaLabel="Replacement owner"
        fullWidth
      /></Field
    >
    {#if error}<p class="banner error">{error}</p>
      <Button onclick={() => onChanged()}>Reload group details</Button>{/if}
  {/snippet}
</ConfirmDialog>

<style>
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }
  .member-list li {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    border-bottom: 1px solid var(--border-subtle);
    padding-block: var(--space-2);
  }
  .member-list li:last-child {
    border-bottom: none;
  }
  .member-name {
    flex: 1;
    min-width: 0;
  }
  .form-fields p {
    margin: 0;
  }
  /* The default p and ul margins stacked on the column gap and left a blank band above the list. */
  .disclosure {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    padding: var(--space-3);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--surface-raised);
    font-size: var(--text-sm);
  }
  .disclosure ul {
    margin: 0;
    padding-left: var(--space-4);
  }
  .disclosure li {
    overflow-wrap: anywhere;
  }
</style>
