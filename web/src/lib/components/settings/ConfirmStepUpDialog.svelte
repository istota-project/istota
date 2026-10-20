<script lang="ts" module>
  import { GroupApiError } from '$lib/api';

  /** A refused code or password: the step-up dialog shows it and stays open. */
  export function isStepUpRefusal(e: unknown): boolean {
    return e instanceof GroupApiError && e.status === 403 && !!e.reason;
  }
</script>

<script lang="ts">
  import type { StepUpAction, StepUpProof } from '$lib/api';
  import { ConfirmDialog } from '$lib/components/ui';
  import StepUpDialog from './StepUpDialog.svelte';

  /**
   * An admin's destructive act behind two gates: a typed-name confirmation,
   * then a step-up proof. `onConfirm` runs with the proof. A step-up refusal
   * stays in the step-up dialog to be corrected; any other failure goes to
   * `onFailed` and the dialog closes, as a success does. `onSettled` runs
   * after either, to reload what the act changed.
   */
  interface Props {
    open: boolean;
    title: string;
    message: string;
    challenge: string;
    confirmLabel?: string;
    action: StepUpAction;
    onConfirm: (proof: StepUpProof) => Promise<unknown>;
    onFailed: (e: unknown) => void;
    onSettled?: () => void | Promise<void>;
    onCancel?: () => void;
  }

  let {
    open = $bindable(false),
    title,
    message,
    challenge,
    confirmLabel = 'Delete',
    action,
    onConfirm,
    onFailed,
    onSettled,
    onCancel,
  }: Props = $props();

  let stage = $state<'confirm' | 'step-up'>('confirm');

  $effect(() => {
    if (!open) stage = 'confirm';
  });

  function close() {
    open = false;
    onCancel?.();
  }

  async function submit(proof: StepUpProof) {
    try {
      await onConfirm(proof);
    } catch (e) {
      if (isStepUpRefusal(e)) throw e;
      onFailed(e);
    }
  }

  async function finish() {
    // Read before closing: a parent may unmount this on close.
    const settle = onSettled;
    open = false;
    await settle?.();
  }
</script>

{#if open && stage === 'confirm'}
  <ConfirmDialog
    open={true}
    {title}
    {message}
    {confirmLabel}
    confirmVariant="danger"
    {challenge}
    onConfirm={() => {
      stage = 'step-up';
      return false;
    }}
    onCancel={close}
  />
{:else if open && stage === 'step-up'}
  <StepUpDialog {action} onConfirm={submit} onComplete={() => void finish()} onCancel={close} />
{/if}
