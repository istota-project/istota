<script lang="ts">
  import { onMount } from 'svelte';
  import { startStepUp, type StepUpAction, type StepUpMethod, type StepUpProof } from '$lib/api';
  import { Button, ConfirmDialog, Field, Input } from '$lib/components/ui';

  /**
   * The step-up half (which methods the account has, starting a request,
   * switching between a password and a mailed code) is this component's;
   * the dialog, the pending state and the in-dialog error are
   * `ConfirmDialog`'s async shell.
   */
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
  let startError = $state('');
  let starting = $state(false);
  let active = true;

  // A refused start keeps whatever request is already open: a spent code
  // budget leaves the password request live, and it can still confirm.
  // Returns the refusal; `quiet` leaves reporting it to the caller.
  async function begin(requested?: StepUpMethod, quiet = false): Promise<string> {
    starting = true;
    try {
      const result = requested
        ? await startStepUp(action, name, requested)
        : await startStepUp(action, name);
      if (!active) return '';
      requestId = result.request_id;
      method = result.method;
      methods = result.methods ?? [result.method];
      hint = result.email_hint ?? '';
      code = '';
      password = '';
      startError = '';
      return '';
    } catch (e) {
      const message =
        (e as Error).message === 'step_up_unavailable'
          ? 'This needs a code sent to your sign-in email address, and your account has none. Ask the operator to add one.'
          : (e as Error).message;
      if (!quiet) startError = message;
      return message;
    } finally {
      starting = false;
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

  // Send new code with a live code request; Try again with none (the start
  // failed, or no method yet), the only way on.
  let secondary = $derived(
    method === 'email'
      ? { label: 'Send new code', method: 'email' as const }
      : !requestId && !starting
        ? { label: 'Try again', method: undefined }
        : null,
  );

  async function submitProof() {
    startError = '';
    const proof: StepUpProof =
      method === 'password' ? { request_id: requestId, password } : { request_id: requestId, code };
    try {
      await onConfirm(proof);
    } catch (e) {
      if (!active) return false;
      password = '';
      if ((e as Error & { reason?: string }).reason === 'dead') {
        // Known dead: a refused restart must not leave it to be resubmitted.
        requestId = '';
        code = '';
        // A restart that is refused too says why: that is what to act on now.
        const refused = await begin(method || undefined, true);
        if (refused) throw new Error(refused);
      }
      throw e;
    }
    code = '';
    password = '';
    if (active) onComplete();
  }

  async function restart(requested?: StepUpMethod) {
    await begin(requested);
    return false;
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

<ConfirmDialog
  open={true}
  title="Confirm sensitive action"
  confirmLabel="Confirm"
  busyLabel="Checking…"
  confirmVariant="primary"
  confirmDisabled={!ready || starting}
  width="420px"
  error={startError}
  secondaryLabel={secondary?.label}
  secondaryBusyLabel="Sending…"
  onSecondary={secondary ? () => restart(secondary.method) : undefined}
  onConfirm={submitProof}
  {onCancel}
>
  {#snippet body()}
    {#if method === 'password'}
      {#if methods.includes('email')}
        <p>Confirm with your account password.</p>
      {:else}
        <p>Mail isn't set up on this install, so confirm with your password.</p>
      {/if}
    {:else if hint}<p>A confirmation code was requested for {hint}.</p>{/if}
    {#if method === 'password'}
      <Field label="Account password">
        <Input type="password" bind:value={password} autocomplete="current-password" />
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
          disabled={starting}
          onclick={() => alternative && begin(alternative.method)}>{alternative.label}</Button
        >
      </div>
    {/if}
  {/snippet}
</ConfirmDialog>

<style>
  .alternative {
    margin-top: var(--space-2);
  }
</style>
