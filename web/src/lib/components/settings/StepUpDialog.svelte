<script lang="ts">
  import { onMount } from 'svelte';
  import { startStepUp, type StepUpAction, type StepUpMethod, type StepUpProof } from '$lib/api';
  import { Button, Field, Input, Modal } from '$lib/components/ui';

  let {
    action,
    name,
    onConfirm,
    onComplete = () => {},
    onCancel,
  }: {
    action: StepUpAction;
    name?: string;
    onConfirm: (proof: StepUpProof) => Promise<unknown>;
    onComplete?: () => void;
    onCancel: () => void;
  } = $props();
  let requestId = $state('');
  let method = $state<StepUpMethod | ''>('');
  let hint = $state('');
  let code = $state('');
  let password = $state('');
  let error = $state('');
  let busy = $state(false);
  let active = true;

  async function requestCode() {
    busy = true;
    code = '';
    password = '';
    requestId = '';
    try {
      const result = await startStepUp(action, name);
      if (!active) return;
      requestId = result.request_id;
      // The server picks the method: a mailed code, or the account password
      // where the install cannot mail one.
      method = result.method;
      hint = result.method === 'email' ? result.email_hint : '';
    } catch (e) {
      error =
        (e as Error).message === 'step_up_unavailable'
          ? 'This needs a code sent to your sign-in email address, and your account has none. Ask the operator to add one.'
          : (e as Error).message;
    } finally {
      busy = false;
    }
  }

  let ready = $derived(
    !!requestId && (method === 'password' ? password.length > 0 : /^[0-9]{6}$/.test(code)),
  );

  async function submitProof() {
    if (busy || !ready) return;
    busy = true;
    error = '';
    const proof: StepUpProof =
      method === 'password' ? { request_id: requestId, password } : { request_id: requestId, code };
    try {
      await onConfirm(proof);
      code = '';
      password = '';
      if (active) onComplete();
    } catch (e) {
      if (!active) return;
      error = (e as Error).message;
      password = '';
      if ((e as Error & { reason?: string }).reason === 'dead') await requestCode();
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    void requestCode();
    return () => {
      active = false;
      code = '';
      password = '';
      requestId = '';
    };
  });
</script>

<Modal
  open={true}
  title="Confirm sensitive action"
  onOpenChange={(open) => {
    if (!open) onCancel();
  }}
>
  {#if method === 'password'}
    <p>Mail isn't set up on this install, so confirm with your password.</p>
  {:else if hint}<p>A confirmation code was requested for {hint}.</p>{/if}
  {#if error}<p class="form-error" role="alert">{error}</p>{/if}
  {#if method === 'password'}
    <Field label="Account password">
      <Input
        type="password"
        bind:value={password}
        autocomplete="current-password"
        onkeydown={(e) => {
          if (e.key === 'Enter') void submitProof();
        }}
      />
    </Field>
  {:else if method === 'email'}
    <Field label="Confirmation code">
      <Input bind:value={code} inputmode="numeric" autocomplete="one-time-code" maxlength={6} />
    </Field>
  {/if}
  {#snippet footer()}
    <Button variant="ghost" onclick={onCancel}>Cancel</Button>
    {#if method === 'email'}
      <Button variant="secondary" disabled={busy} onclick={requestCode}>Send new code</Button>
    {:else if !requestId && !busy}
      <!-- No live request (the start failed, or no method yet): the only way on. -->
      <Button
        variant="secondary"
        onclick={() => {
          error = '';
          void requestCode();
        }}>Try again</Button
      >
    {/if}
    <Button
      variant="primary"
      loading={busy}
      loadingLabel="Checking…"
      disabled={!ready}
      onclick={submitProof}>Confirm</Button
    >
  {/snippet}
</Modal>
