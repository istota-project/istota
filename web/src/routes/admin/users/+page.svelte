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
    Modal,
    KebabMenu,
    NoticeBanner,
    type KebabItem,
  } from '$lib/components/ui';
  import { SettingsCard } from '$lib/components/settings';
  import { formatRelative } from '$lib/dateFormat';
  import UserCell from '$lib/admin/UserCell.svelte';

  let view = $state<AdminUsers | null>(null);
  let error = $state('');
  let notice = $state('');
  let busy = $state(false);
  let userId = $state('');
  let email = $state('');
  let displayName = $state('');
  let attaching = $state(false);
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
    if (!attaching && !/^[a-z0-9][a-z0-9._-]{0,31}$/.test(userId)) {
      formError =
        'Use 1–32 lowercase letters, digits, dots, underscores or hyphens, starting with a letter or digit.';
      return;
    }
    busy = true;
    try {
      await createAdminUser({ user_id: userId, email, display_name: displayName });
      notice = 'Identity saved and invitation sent.';
      userId = email = displayName = '';
      attaching = false;
      formOpen = false;
    } catch (e) {
      if (e instanceof AuthError) failed(e);
      else formError = e instanceof Error ? e.message : 'The user operation failed.';
    } finally {
      await load();
      busy = false;
    }
  }

  function attach(user: AdminUser) {
    if (busy) return;
    userId = user.user_id;
    email = user.identity?.email ?? '';
    displayName = '';
    attaching = true;
    error = notice = '';
    formError = '';
    formOpen = true;
  }

  function add() {
    userId = email = displayName = '';
    error = notice = formError = '';
    attaching = false;
    formOpen = true;
  }

  function userActions(user: AdminUser): KebabItem[] {
    if (!user.identity)
      return [
        {
          label: 'Attach email',
          disabled: busy || !view?.email_enabled,
          onSelect: () => attach(user),
        },
      ];
    const mailDisabled = busy || !view?.email_enabled || user.identity?.disabled;
    return [
      { label: 'Send invitation', disabled: mailDisabled, onSelect: () => act(user, 'invite') },
      {
        label: 'Send sign-in link',
        disabled: mailDisabled,
        onSelect: () => act(user, 'login-link'),
      },
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
      notice = ['invite', 'login-link', 'reset'].includes(action)
        ? 'Link sent.'
        : 'Identity updated.';
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

<div class="settings admin-page">
  {#if !view && !error}
    <div class="center-msg">Loading users…</div>
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
        <p class="empty">No users yet. Add a user to send their first invitation.</p>
      {:else}
        <div class="table-scroll">
          <table class="grid users-grid" aria-label="Users">
            <thead
              ><tr
                ><th scope="col" class="col-user">User</th>
                <th scope="col">Email</th><th scope="col" class="access">Sign-in</th><th
                  scope="col"
                  class="last-login">Last login</th
                ><th scope="col" class="user-actions" aria-label="Actions"></th></tr
              ></thead
            >
            <tbody>
              {#each view.users as user (user.user_id)}
                <tr>
                  <td>
                    <UserCell
                      userId={user.user_id}
                      displayName={user.display_name}
                      isAdmin={user.is_admin}
                    />
                  </td>
                  <td class="email-cell" title={user.identity?.email}
                    >{user.identity?.email ?? '—'}</td
                  >
                  <td class="access">
                    {#if user.identity?.disabled}<Badge variant="partial">Disabled</Badge>{:else}
                      <span
                        >{user.state === 'nextcloud_only'
                          ? 'Nextcloud only'
                          : user.state === 'passwordless'
                            ? 'Sign-in link'
                            : 'Password set'}</span
                      >
                    {/if}
                  </td>
                  <td class="last-login"
                    >{user.identity
                      ? user.identity.last_login_at
                        ? formatRelative(user.identity.last_login_at)
                        : 'Never'
                      : '—'}</td
                  >
                  <td class="user-actions">
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
              <div class="user-detail">{user.identity?.email}</div>
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

<Modal
  bind:open={formOpen}
  title={attaching ? 'Attach email' : 'Add user'}
  description={attaching
    ? `Set up email sign-in for ${userId}.`
    : 'Send an invitation to set up web access.'}
>
  <form aria-label={attaching ? 'Attach email' : 'Add user'} onsubmit={submit} class="user-form">
    {#if formError}<p class="banner error" role="alert">{formError}</p>{/if}
    <Field
      label="User ID"
      warning={attaching
        ? 'The existing ID is preserved. Attaching email ends older Nextcloud sessions.'
        : 'This becomes a directory name: 1–32 lowercase letters, digits, dots, underscores or hyphens; start with a letter or digit.'}
    >
      <Input
        aria-label="User ID"
        bind:value={userId}
        required
        readonly={attaching}
        disabled={busy}
      />
    </Field>
    <Field label="Email"><Input type="email" bind:value={email} required disabled={busy} /></Field>
    {#if !attaching}
      <Field label="Display name (optional)"
        ><Input bind:value={displayName} disabled={busy} /></Field
      >
      <p class="form-note">
        New users can sign in immediately. Their background work starts after the next daemon
        reload.
      </p>
    {/if}
    <div class="form-actions dialog-actions">
      <Button variant="ghost" onclick={() => (formOpen = false)} disabled={busy}>Cancel</Button>
      <Button
        type="submit"
        variant="primary"
        loading={busy}
        loadingLabel="Sending…"
        disabled={!view?.email_enabled}>{attaching ? 'Attach and invite' : 'Add and invite'}</Button
      >
    </div>
  </form>
</Modal>

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
    color: var(--text-muted);
    font-size: var(--text-sm);
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
    color: var(--text-muted);
    font-size: var(--text-xs);
    overflow-wrap: anywhere;
  }
  .access {
    width: 8rem;
  }
  .last-login {
    width: 7rem;
  }
  td.last-login {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .users-grid .user-actions {
    width: 2.5rem;
    text-align: right;
  }
  .orphan-row {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: var(--space-3);
  }
  .col-user {
    width: 11rem;
  }
  .email-cell {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  @container settings (max-width: 40rem) {
    .last-login {
      display: none;
    }
    .col-user {
      width: 30%;
    }
    .access {
      width: 25%;
    }
  }
</style>
