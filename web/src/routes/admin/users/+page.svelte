<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    AuthError,
    getAdminUsers,
    createAdminUser,
    adminUserAction,
    type AdminUsers,
    type AdminUser,
    type AdminUserAction,
  } from '$lib/api';
  import {
    Button,
    Input,
    Field,
    Badge,
    ConfirmDialog,
    EmptyState,
    Modal,
    KebabMenu,
    NoticeBanner,
    type KebabItem,
    PageLoading,
  } from '$lib/components/ui';
  import { SettingsCard } from '$lib/components/settings';
  import { formatRelative } from '$lib/dateFormat';
  import UserCell from '$lib/admin/UserCell.svelte';
  import { signInStateLabel } from '$lib/admin/signInState';
  import AdminUserModal from './AdminUserModal.svelte';

  let view = $state<AdminUsers | null>(null);
  let error = $state('');
  let notice = $state('');
  let busy = $state(false);
  let userId = $state('');
  let email = $state('');
  let displayName = $state('');
  /* The user whose settings editor is open, or null. */
  let editing = $state<string | null>(null);
  let formOpen = $state(false);
  let formError = $state('');
  let aboutCollapsed = $state(true);
  let confirmOpen = $state(false);
  let pending = $state<{ user: AdminUser; action: AdminUserAction } | null>(null);
  const disableCopy =
    'Disable blocks email and Nextcloud sign-in and ends existing web sessions. It does not stop scheduled work.';
  const removeCopy =
    'Remove withdraws email login and ends existing web sessions. A fresh Nextcloud sign-in is still allowed when Nextcloud is enabled. The profile and user data are kept.';

  function failed(e: unknown) {
    if (e instanceof AuthError) window.location.assign(`${base}/login`);
    else error = e instanceof Error ? e.message : 'The user operation failed.';
  }

  async function load() {
    try {
      view = await getAdminUsers();
    } catch (e) {
      failed(e);
    }
  }
  onMount(load);

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    if (busy) return;
    formError = '';
    if (!/^[a-z0-9][a-z0-9._-]{0,31}$/.test(userId)) {
      formError =
        'Use 1–32 lowercase letters, digits, dots, underscores or hyphens, starting with a letter or digit.';
      return;
    }
    busy = true;
    try {
      await createAdminUser({ user_id: userId, email, display_name: displayName });
      notice = 'Identity saved and invitation sent.';
      const created = userId;
      userId = email = displayName = '';
      formOpen = false;
      // The editor holds everything the create form does not ask for.
      editing = created;
    } catch (e) {
      if (e instanceof AuthError) failed(e);
      else formError = e instanceof Error ? e.message : 'The user operation failed.';
    } finally {
      await load();
      busy = false;
    }
  }

  function edit(user: AdminUser) {
    error = notice = '';
    editing = user.user_id;
  }

  function add() {
    userId = email = displayName = '';
    error = notice = formError = '';
    formOpen = true;
  }

  function userActions(user: AdminUser): KebabItem[] {
    const editItem: KebabItem = { label: 'Edit settings', onSelect: () => edit(user) };
    // A login email is attached from the editor's Login section.
    if (!user.identity) return [editItem];
    const mailDisabled = busy || !view?.email_enabled || user.identity?.disabled;
    return [
      editItem,
      { label: 'Send invitation', disabled: mailDisabled, onSelect: () => act(user, 'invite') },
      { label: 'Send password reset', disabled: mailDisabled, onSelect: () => act(user, 'reset') },
      {
        label: 'Sign out everywhere',
        disabled: busy,
        onSelect: () => requestConfirmation(user, 'logout-all'),
      },
      {
        label: user.identity?.disabled ? 'Enable web access' : 'Disable web access',
        disabled: busy,
        onSelect: () =>
          user.identity?.disabled ? act(user, 'enable') : requestConfirmation(user, 'disable'),
      },
      {
        label: 'Remove email login',
        danger: true,
        disabled: busy,
        onSelect: () => requestConfirmation(user, 'remove'),
      },
    ];
  }

  async function act(user: AdminUser, action: AdminUserAction) {
    if (busy) return;
    busy = true;
    error = notice = '';
    confirmOpen = false;
    try {
      await adminUserAction(user.user_id, action);
      notice = ['invite', 'reset'].includes(action) ? 'Link sent.' : 'Identity updated.';
    } catch (e) {
      failed(e);
    } finally {
      await load();
      busy = false;
    }
  }

  function requestConfirmation(user: AdminUser, action: AdminUserAction) {
    pending = { user, action };
    confirmOpen = true;
  }

  const confirmationTitle = $derived(
    pending?.action === 'remove'
      ? 'Remove email login'
      : pending?.action === 'logout-all'
        ? 'Sign out everywhere'
        : 'Disable web access',
  );
  const confirmationCopy = $derived(
    pending?.action === 'remove'
      ? removeCopy
      : pending?.action === 'logout-all'
        ? 'End all email and Nextcloud web sessions. Nextcloud service credentials are kept.'
        : disableCopy,
  );
</script>

<div class="settings admin-page card-pane">
  {#if !view && !error}
    <PageLoading label="Loading users…" />
  {:else if !view}
    <div class="center-msg error">{error}</div>
  {:else}
    {#if error}<p class="banner error" role="alert">{error}</p>{/if}
    {#if notice}<p class="banner success" role="status">{notice}</p>{/if}
    {#if !view.email_enabled}
      <p class="banner warn">
        Email sign-in is not enabled. Sending links and attaching email are unavailable.
      </p>
    {/if}
    <NoticeBanner title="About web access" bind:collapsed={aboutCollapsed}>
      <p>{disableCopy}</p>
      <p>{removeCopy}</p>
      <p>
        Sign out everywhere ends email and Nextcloud web sessions. These controls do not revoke
        Nextcloud service credentials.
      </p>
    </NoticeBanner>
    <SettingsCard title="Users">
      {#snippet actions()}
        <Button variant="primary" size="sm" onclick={add} disabled={busy || !view?.email_enabled}
          >Add user</Button
        >
      {/snippet}
      {#if view.users.length === 0}
        <EmptyState>No users yet. Add a user to send their first invitation.</EmptyState>
      {:else}
        <div class="table-scroll">
          <table
            class="table table--fixed table--interactive table--on-card users-grid"
            aria-label="Users"
          >
            <thead
              ><tr
                ><th scope="col" class="col-user">User</th>
                <th scope="col" class="col-email">Email</th><th scope="col" class="col-access"
                  >Sign-in</th
                ><th scope="col" class="col-last-login">Last login</th><th
                  scope="col"
                  class="col-actions"
                  aria-label="Actions"
                ></th></tr
              ></thead
            >
            <tbody>
              {#each view.users as user (user.user_id)}
                <!-- The row is a shortcut; the kebab's "Edit settings" is the
                     keyboard route to the same editor. -->
                <tr class="row-clickable" onclick={() => edit(user)}>
                  <td>
                    <UserCell
                      userId={user.user_id}
                      displayName={user.display_name}
                      isAdmin={user.is_admin}
                    />
                  </td>
                  <td class="col-email truncate" title={user.identity?.email}
                    >{user.identity?.email ?? '—'}</td
                  >
                  <td class="col-access">
                    <Badge size="sm" variant={user.identity?.disabled ? 'partial' : 'neutral'}
                      >{signInStateLabel(user.identity, user.state)}</Badge
                    >
                  </td>
                  <td class="col-last-login truncate"
                    >{user.identity
                      ? user.identity.last_login_at
                        ? formatRelative(user.identity.last_login_at)
                        : 'Never'
                      : '—'}</td
                  >
                  <td class="col-actions">
                    <KebabMenu
                      items={userActions(user)}
                      ariaLabel="Actions for {user.display_name || user.user_id}"
                    />
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </SettingsCard>
    {#if view.orphans.length}
      <SettingsCard
        title="Identities without profiles"
        description="These accounts cannot sign in. Restore their profiles with the operator CLI or remove their email login."
      >
        {#each view.orphans as user (user.user_id)}
          <div class="orphan-row">
            <div>
              <div class="user-name">{user.user_id}</div>
              <div class="user-detail caption">{user.identity?.email}</div>
            </div>
            <Button
              variant="danger"
              size="sm"
              disabled={busy}
              onclick={() => requestConfirmation(user, 'remove')}>Remove email login</Button
            >
          </div>
        {/each}
      </SettingsCard>
    {/if}
  {/if}
</div>

<Modal bind:open={formOpen} title="Add user" description="Send an invitation to set up web access.">
  <form aria-label="Add user" onsubmit={submit} class="user-form">
    {#if formError}<p class="banner error" role="alert">{formError}</p>{/if}
    <Field
      label="User ID"
      warning="This becomes a directory name: 1–32 lowercase letters, digits, dots, underscores or hyphens; start with a letter or digit."
    >
      <Input aria-label="User ID" bind:value={userId} required disabled={busy} />
    </Field>
    <Field label="Email"><Input type="email" bind:value={email} required disabled={busy} /></Field>
    <Field label="Display name (optional)"><Input bind:value={displayName} disabled={busy} /></Field
    >
    <p class="form-note description">
      New users can sign in immediately. Their background work starts after the next daemon reload.
    </p>
    <div class="form-actions dialog-actions">
      <Button variant="secondary" onclick={() => (formOpen = false)} disabled={busy}>Cancel</Button>
      <Button
        type="submit"
        variant="primary"
        loading={busy}
        loadingLabel="Sending…"
        disabled={!view?.email_enabled}>Add and invite</Button
      >
    </div>
  </form>
</Modal>

{#if editing}
  <AdminUserModal
    userId={editing}
    onClose={() => (editing = null)}
    onChanged={load}
    onSignedOut={() => window.location.assign(`${base}/login`)}
  />
{/if}

<ConfirmDialog
  bind:open={confirmOpen}
  title={confirmationTitle}
  message={`${pending?.user.display_name || pending?.user.user_id || ''}: ${confirmationCopy}`}
  confirmLabel={pending?.action === 'remove'
    ? 'Remove'
    : pending?.action === 'logout-all'
      ? 'Sign out'
      : 'Disable'}
  confirmDisabled={busy}
  onConfirm={() => {
    if (pending) act(pending.user, pending.action);
  }}
/>

<style>
  .user-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
  }
  .dialog-actions {
    padding-top: var(--space-3);
    border-top: 1px solid var(--border-subtle);
  }
  .form-note {
    margin: 0;
  }
  .user-name {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    overflow-wrap: anywhere;
  }
  .user-detail {
    margin-top: var(--space-1);
    overflow-wrap: anywhere;
  }
  .row-clickable {
    cursor: pointer;
  }
  .orphan-row {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: var(--space-3);
  }
  /* Email is the unsized column: the min-width reserves it 10rem past the
     sized ones (the actions column is table.css's 2.5rem), and
     adminTables.test.ts holds the sum. */
  .users-grid {
    min-width: 38.5rem;
  }
  .col-user {
    width: 11rem;
  }
  .col-access {
    width: 8rem;
  }
  .col-last-login {
    width: 7rem;
  }
  /* Narrow: Last login goes and the rest tighten to what a phone has, about
     18.5rem inside the card at 375px, so the table fits rather than scrolls. */
  @container settings (max-width: 40rem) {
    .users-grid {
      min-width: 18.5rem;
    }
    .col-last-login {
      display: none;
    }
    .col-user {
      width: 5.5rem;
    }
    .col-access {
      width: 5rem;
    }
  }
</style>
