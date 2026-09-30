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
  <SettingsCard title="Security">
    <p>Login email: <strong>{auth.email}</strong></p>
    {#if auth.can_change_password}
      <p>Changing your password signs you out of every session, including this tab.</p>
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
        <div>
          <Button type="submit" variant="primary" loading={busy} loadingLabel="Changing password…"
            >Change password</Button
          >
        </div>
      </form>
    {:else}
      <p>
        You sign in with an email link. To set a password, request a reset link using your login
        email above.
      </p>
      <Button href="{base}/auth/reset" variant="secondary">Set a password</Button>
    {/if}
  </SettingsCard>
{/if}

<style>
  .password-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
</style>
