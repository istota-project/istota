<script lang="ts">
  import { onMount } from 'svelte';
  import { startStepUp, type StepUpAction, type StepUpProof } from '$lib/api';
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
  let hint = $state('');
  let code = $state('');
  let error = $state('');
  let busy = $state(false);
  let active = true;

  async function requestCode() {
    busy = true;
    code = '';
    requestId = '';
    try {
      const result = await startStepUp(action, name);
      if (!active) return;
      requestId = result.request_id;
      hint = result.email_hint;
    } catch (e) {
      error =
        (e as Error).message === 'step_up_unavailable'
          ? 'This needs a code sent to your sign-in email address, and your account has none. Ask the operator to add one.'
          : (e as Error).message;
    } finally {
      busy = false;
    }
  }

  async function confirm() {
    if (busy || !requestId || !/^[0-9]{6}$/.test(code)) return;
    busy = true;
    error = '';
    try {
      await onConfirm({ request_id: requestId, code });
      code = '';
      if (active) onComplete();
    } catch (e) {
      if (!active) return;
      error = (e as Error).message;
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
  {#if hint}<p>A confirmation code was requested for {hint}.</p>{/if}
  {#if error}<p class="form-error" role="alert">{error}</p>{/if}
  <Field label="Confirmation code">
    <Input bind:value={code} inputmode="numeric" autocomplete="one-time-code" maxlength={6} />
  </Field>
  {#snippet footer()}
    <Button variant="ghost" onclick={onCancel}>Cancel</Button>
    <Button variant="secondary" disabled={busy} onclick={requestCode}>Send new code</Button>
    <Button
      variant="primary"
      loading={busy}
      loadingLabel="Checking…"
      disabled={!requestId || !/^[0-9]{6}$/.test(code)}
      onclick={confirm}>Confirm</Button
    >
  {/snippet}
</Modal>
