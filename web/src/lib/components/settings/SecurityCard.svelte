<script lang="ts">
  import { base } from '$app/paths';
  import { AuthError, changePassword, type UserAuth } from '$lib/api';
  import { Button, Input } from '$lib/components/ui';
  import SettingsCard from './SettingsCard.svelte';
  import SettingsField from './SettingsField.svelte';

  let { auth, onSignedOut }: { auth?: UserAuth; onSignedOut: () => void } = $props();
  let currentPassword = $state('');
  let newPassword = $state('');
  let confirmPassword = $state('');
  let busy = $state(false);
  let error = $state('');

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    if (busy) return;
    error = '';
    if (newPassword !== confirmPassword) {
      error = 'Passwords must match.';
      return;
    }
    busy = true;
    try {
      await changePassword(currentPassword, newPassword);
      currentPassword = newPassword = confirmPassword = '';
      onSignedOut();
    } catch (e) {
      if (e instanceof AuthError) onSignedOut();
      else error = (e as Error).message || 'Password could not be changed.';
    } finally {
      busy = false;
    }
  }
</script>

{#if auth?.method === 'email'}
  <SettingsCard
    title="Security"
    description={auth.can_change_password
      ? 'Changing your password signs you out of every session, including this tab.'
      : 'You sign in with an email link. To set a password, request a reset link using your login email below.'}
  >
    <SettingsField label="Login email">
      <Input type="email" value={auth.email ?? ''} autocomplete="username" readonly />
    </SettingsField>
    {#if auth.can_change_password}
      <form onsubmit={submit} class="password-form">
        <SettingsField label="Current password">
          <Input
            type="password"
            autocomplete="current-password"
            bind:value={currentPassword}
            required
            disabled={busy}
          />
        </SettingsField>
        <SettingsField label="New password">
          <Input
            type="password"
            autocomplete="new-password"
            bind:value={newPassword}
            required
            disabled={busy}
          />
        </SettingsField>
        <SettingsField label="Confirm new password">
          <Input
            type="password"
            autocomplete="new-password"
            bind:value={confirmPassword}
            required
            disabled={busy}
          />
        </SettingsField>
        {#if error}<p class="banner error" role="alert">{error}</p>{/if}
        <div class="actions">
          <Button
            type="submit"
            variant="primary"
            size="sm"
            loading={busy}
            loadingLabel="Changing password…">Change password</Button
          >
        </div>
      </form>
    {:else}
      <div class="actions">
        <Button href="{base}/auth/reset" variant="secondary" size="sm">Set a password</Button>
      </div>
    {/if}
  </SettingsCard>
{/if}

<style>
  .password-form {
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
</style>
