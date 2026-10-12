<script lang="ts">
  import { SecretField } from '$lib/components/settings';
  import type { SetupDraftStore } from './setupDraft.svelte';

  interface Props {
    draft: SetupDraftStore;
    errors: Record<string, string>;
  }

  let { draft, errors }: Props = $props();

  // Both fields are held as scratch until they match: a password goes into
  // the draft only confirmed, so a Next on another screen cannot save it.
  const TYPED = 'password';
  const CONFIRM = 'password_confirm';

  const email = $derived(draft.text('user_email'));

  /** Before Next: a typed password must be typed twice. The rule on what
   *  makes a good password is the server's, in `/validate`. */
  export function precheck(): Record<string, string> | null {
    const typed = draft.scratch[TYPED] ?? '';
    if (!typed) return null;
    if (typed !== (draft.scratch[CONFIRM] ?? '')) {
      return { [CONFIRM]: "The two passwords don't match." };
    }
    draft.setSecret('password', typed);
    draft.scratch[TYPED] = '';
    draft.scratch[CONFIRM] = '';
    return null;
  }
</script>

<div class="step">
  <p>
    {#if email}
      You will sign in with your email, <strong>{email}</strong>, and this password.
    {:else}
      You will sign in with your email and this password.
    {/if}
    Use one you don't use anywhere else.
  </p>
  <SecretField
    label="Password"
    configured={draft.isSet('password')}
    value={draft.scratch[TYPED] ?? ''}
    error={errors.password}
    onValueChange={(value) => (draft.scratch[TYPED] = value)}
  />
  <SecretField
    label="Confirm password"
    configured={draft.isSet('password')}
    value={draft.scratch[CONFIRM] ?? ''}
    error={errors[CONFIRM]}
    onValueChange={(value) => (draft.scratch[CONFIRM] = value)}
  />
</div>

<style>
  .step {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  p {
    margin: 0;
  }
</style>
