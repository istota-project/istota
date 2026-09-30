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
  import { Button, Input, Field, Badge, ConfirmDialog } from '$lib/components/ui';

  let view = $state<AdminUsers | null>(null);
  let error = $state('');
  let notice = $state('');
  let busy = $state(false);
  let userId = $state('');
  let email = $state('');
  let displayName = $state('');
  let attaching = $state(false);
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
    error = notice = '';
    if (!attaching && !/^[a-z0-9][a-z0-9._-]{0,31}$/.test(userId)) {
      error =
        'Use 1–32 lowercase letters, digits, dots, underscores or hyphens, starting with a letter or digit.';
      return;
    }
    busy = true;
    try {
      await createAdminUser({ user_id: userId, email, display_name: displayName });
      notice = 'Identity saved and invitation sent.';
      userId = email = displayName = '';
      attaching = false;
    } catch (e) {
      failed(e);
    } finally {
      await load();
      busy = false;
    }
  }

  function attach(user: AdminUser) {
    userId = user.user_id;
    email = user.identity?.email ?? '';
    displayName = '';
    attaching = true;
    error = notice = '';
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
</script>

<div class="settings admin-page">
  {#if !view && !error}
    <div class="center-msg">Loading users…</div>
  {:else if !view}
    <div class="center-msg error">{error}</div>
  {:else}
    {#if error}<p class="banner error" role="alert">{error}</p>{/if}
    {#if notice}<p class="banner success" role="status">{notice}</p>{/if}
    {#if !view.email_enabled}<p class="banner warn">
        Email sign-in is not enabled. Sending links and attaching identities are unavailable.
      </p>{/if}
    <section class="card">
      <h2>{attaching ? 'Attach email' : 'Add user'}</h2>
      <p>
        New users can sign in immediately. Their background work starts after the next daemon
        reload.
      </p>
      <form
        aria-label={attaching ? 'Attach email' : 'Add user'}
        onsubmit={submit}
        class="user-form"
      >
        <p class="caption">
          {attaching
            ? 'The existing ID is preserved. Attaching an identity ends older Nextcloud sessions.'
            : 'This becomes a directory name: 1–32 lowercase letters, digits, dots, underscores or hyphens; start with a letter or digit.'}
        </p>
        <Field label="User ID">
          <Input
            bind:value={userId}
            required
            readonly={attaching}
            disabled={busy || !view.email_enabled}
          />
        </Field>
        <Field label="Email"
          ><Input
            type="email"
            bind:value={email}
            required
            disabled={busy || !view.email_enabled}
          /></Field
        >
        {#if !attaching}<Field label="Display name (optional)"
            ><Input bind:value={displayName} disabled={busy || !view.email_enabled} /></Field
          >{/if}
        <div class="actions">
          <Button type="submit" variant="primary" loading={busy} disabled={!view.email_enabled}
            >{attaching ? 'Attach and invite' : 'Add and invite'}</Button
          >
          {#if attaching}<Button
              onclick={() => {
                attaching = false;
                userId = email = displayName = '';
              }}
              disabled={busy}>Cancel</Button
            >{/if}
        </div>
      </form>
    </section>
    <section class="card">
      <h2>Web access</h2>
      <p>{disableCopy}</p>
      <p>{removeCopy}</p>
      <p>
        Sign out everywhere ends email and Nextcloud web sessions. These controls do not revoke
        Nextcloud service credentials.
      </p>
    </section>
    {#each view.users as user (user.user_id)}
      <section class="card" aria-label={user.user_id}>
        <div class="card-head">
          <h2>{user.display_name} <span class="muted">({user.user_id})</span></h2>
          {#if user.is_admin}<Badge>admin</Badge>{/if}
        </div>
        <p>
          {user.state === 'nextcloud_only'
            ? 'Nextcloud only'
            : user.state === 'passwordless'
              ? 'No password; sign-in links available'
              : 'Password set'}
        </p>
        {#if user.identity}
          <p>
            {user.identity.email}
            {#if user.identity.disabled}<Badge variant="partial">Disabled</Badge>{/if}
          </p>
          <p class="caption">Last login: {user.identity.last_login_at ?? 'Never'}</p>
          <div class="actions">
            <Button
              disabled={busy || !view.email_enabled || user.identity.disabled}
              onclick={() => act(user, 'invite')}>Send invitation</Button
            >
            <Button
              disabled={busy || !view.email_enabled || user.identity.disabled}
              onclick={() => act(user, 'login-link')}>Send sign-in link</Button
            >
            <Button
              disabled={busy || !view.email_enabled || user.identity.disabled}
              onclick={() => act(user, 'reset')}>Send password reset</Button
            >
            <Button
              disabled={busy}
              onclick={() =>
                user.identity?.disabled
                  ? act(user, 'enable')
                  : requestConfirmation(user, 'disable')}
              >{user.identity.disabled ? 'Enable' : 'Disable'}</Button
            >
            <Button disabled={busy} onclick={() => act(user, 'logout-all')}
              >Sign out everywhere</Button
            >
            <Button
              variant="danger"
              disabled={busy}
              onclick={() => requestConfirmation(user, 'remove')}>Remove email login</Button
            >
          </div>
        {:else}
          <p class="caption">
            Attach an email identity to enable disable and sign-out-everywhere controls.
          </p>
          <Button disabled={busy || !view.email_enabled} onclick={() => attach(user)}
            >Attach email</Button
          >
        {/if}
      </section>
    {/each}
    {#if view.orphans.length}
      <section class="card">
        <h2>Identities without profiles</h2>
        <p>
          These identities cannot sign in. Repair their profiles with the operator CLI or remove
          their email login.
        </p>
        {#each view.orphans as user (user.user_id)}
          <div class="actions">
            <span>{user.user_id}: {user.identity?.email}</span><Button
              variant="danger"
              disabled={busy}
              onclick={() => requestConfirmation(user, 'remove')}>Remove email login</Button
            >
          </div>
        {/each}
      </section>
    {/if}
  {/if}
</div>

<ConfirmDialog
  bind:open={confirmOpen}
  title={pending?.action === 'remove' ? 'Remove email login' : 'Disable web access'}
  message={pending?.action === 'remove' ? removeCopy : disableCopy}
  confirmLabel={pending?.action === 'remove' ? 'Remove' : 'Disable'}
  confirmDisabled={busy}
  onConfirm={() => {
    if (pending) act(pending.user, pending.action);
  }}
/>

<style>
  .user-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
  }
  .card-head {
    align-items: center;
  }
</style>
