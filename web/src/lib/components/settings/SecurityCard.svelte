<script lang="ts">
  import { base } from '$app/paths';
  import { AuthError, changePassword, type UserAuth } from '$lib/api';
  import { Alert, Button, Input, Card, Field, FormStack } from '@minot/ui';

  let { auth, onSignedOut }: { auth?: UserAuth; onSignedOut: () => void } = $props();
  let currentPassword = $state('');
  let newPassword = $state('');
  let confirmPassword = $state('');
  let busy = $state(false);
  let error = $state('');
  let mismatch = $state('');

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    if (busy) return;
    error = '';
    mismatch = '';
    if (newPassword !== confirmPassword) {
      mismatch = 'Passwords must match.';
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

{#if auth?.can_change_password || auth?.can_set_password}
  <Card
    title="Security"
    description={auth.can_change_password
      ? 'Changing your password signs you out of every session, including this tab.'
      : 'Your account has no password yet. To set one, request a password link using your login email below.'}
  >
    <Field label="Login email">
      <Input type="email" value={auth.email ?? ''} autocomplete="username" readonly />
    </Field>
    {#if auth.can_change_password}
      <FormStack as="form" onsubmit={submit} disabled={busy}>
        <Field label="Current password">
          <Input
            type="password"
            autocomplete="current-password"
            bind:value={currentPassword}
            required
          />
        </Field>
        <Field label="New password">
          <Input type="password" autocomplete="new-password" bind:value={newPassword} required />
        </Field>
        <Field label="Confirm new password" error={mismatch}>
          <Input
            type="password"
            autocomplete="new-password"
            bind:value={confirmPassword}
            required
          />
        </Field>
        {#if error}<Alert appearance="inline" variant="danger" role="alert">{error}</Alert>{/if}
        <div class="actions hstack wrap">
          <Button
            type="submit"
            variant="primary"
            size="sm"
            loading={busy}
            loadingLabel="Changing password…">Change password</Button
          >
        </div>
      </FormStack>
    {:else}
      <div class="actions hstack wrap">
        <Button href="{base}/auth/reset" variant="secondary" size="sm">Set a password</Button>
      </div>
    {/if}
  </Card>
{/if}
