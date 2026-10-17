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
  let methods = $state<StepUpMethod[]>([]);
  let hint = $state('');
  let code = $state('');
  let password = $state('');
  let error = $state('');
  let busy = $state(false);
  let active = true;

  // A refused start keeps whatever request is already open: a spent code
  // budget leaves the password request live, and it can still confirm.
  async function begin(requested?: StepUpMethod) {
    busy = true;
    try {
      const result = requested
        ? await startStepUp(action, name, requested)
        : await startStepUp(action, name);
      if (!active) return;
      requestId = result.request_id;
      method = result.method;
      methods = result.methods ?? [result.method];
      hint = result.email_hint ?? '';
      code = '';
      password = '';
      error = '';
    } catch (e) {
      error =
        (e as Error).message === 'step_up_unavailable'
          ? 'This needs a code sent to your sign-in email address, and your account has none. Ask the operator to add one.'
          : (e as Error).message;
    } finally {
      busy = false;
    }
  }

  let alternative = $derived(
    method === 'password' && methods.includes('email')
      ? { method: 'email' as const, label: 'Email me a code instead' }
      : method === 'email' && methods.includes('password')
        ? { method: 'password' as const, label: 'Use my password instead' }
        : null,
  );

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
      if ((e as Error & { reason?: string }).reason === 'dead') await begin(method || undefined);
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    void begin();
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
    {#if methods.includes('email')}
      <p>Confirm with your account password.</p>
    {:else}
      <p>Mail isn't set up on this install, so confirm with your password.</p>
    {/if}
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
  {#if alternative}
    <div class="alternative">
      <Button
        variant="secondary"
        size="sm"
        disabled={busy}
        onclick={() => alternative && begin(alternative.method)}>{alternative.label}</Button
      >
    </div>
  {/if}
  {#snippet footer()}
    <Button variant="secondary" onclick={onCancel}>Cancel</Button>
    {#if method === 'email'}
      <Button variant="secondary" disabled={busy} onclick={() => begin('email')}
        >Send new code</Button
      >
    {:else if !requestId && !busy}
      <!-- No live request (the start failed, or no method yet): the only way on. -->
      <Button
        variant="secondary"
        onclick={() => {
          error = '';
          void begin();
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

<style>
  .alternative {
    margin-top: var(--space-2);
  }
</style>
