<script lang="ts">
  import type { Snippet } from 'svelte';
  import Modal from './Modal.svelte';
  import Button from './Button.svelte';

  interface Props {
    /** Whether the dialog is shown. Bindable. */
    open: boolean;
    /** Imperative statement, no trailing "?" — e.g. "Delete block". */
    title: string;
    /** The "Are you sure…" body. A plain string, or a snippet for rich markup. */
    message?: string;
    body?: Snippet;
    /** Confirm-button label. */
    confirmLabel?: string;
    cancelLabel?: string;
    /** Confirm-button style. "danger" (red outline) for destructive actions. */
    confirmVariant?: 'danger' | 'primary';
    /**
     * Type-to-confirm challenge. When set, the user must type this exact string
     * (e.g. a room name) before the confirm button enables — the destructive
     * high-friction path (RoomSettings hard delete).
     */
    challenge?: string;
    /** Extra disable condition layered on top of the challenge gate. */
    confirmDisabled?: boolean;
    onConfirm: () => void;
    onCancel?: () => void;
  }

  let {
    open = $bindable(false),
    title,
    message,
    body,
    confirmLabel = 'Delete',
    cancelLabel = 'Cancel',
    confirmVariant = 'danger',
    challenge,
    confirmDisabled = false,
    onConfirm,
    onCancel,
  }: Props = $props();

  let typed = $state('');

  // Reset the challenge field whenever the dialog opens.
  $effect(() => {
    if (open) typed = '';
  });

  const challengeMet = $derived(challenge == null || typed === challenge);
  const canConfirm = $derived(challengeMet && !confirmDisabled);

  function cancel() {
    open = false;
    onCancel?.();
  }

  function confirm() {
    if (!canConfirm) return;
    onConfirm();
  }

  function handleOpenChange(next: boolean) {
    if (!next) cancel();
  }
</script>

<Modal bind:open {title} onOpenChange={handleOpenChange} width="380px">
  {#if body}
    {@render body()}
  {:else if message}
    <p class="confirm-message">{message}</p>
  {/if}

  {#if challenge != null}
    <p class="confirm-challenge">
      Type <code>{challenge}</code> to confirm.
    </p>
    <input
      class="confirm-challenge-input"
      type="text"
      bind:value={typed}
      placeholder={challenge}
      autocomplete="off"
      aria-label="Type to confirm"
    />
  {/if}

  {#snippet footer()}
    <Button variant="ghost" onclick={cancel}>{cancelLabel}</Button>
    <Button variant={confirmVariant} onclick={confirm} disabled={!canConfirm}>{confirmLabel}</Button
    >
  {/snippet}
</Modal>

<style>
  .confirm-message {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-primary);
    line-height: 1.5;
  }
  .confirm-challenge {
    margin: 0.75rem 0 0.35rem;
    font-size: var(--text-sm);
    color: var(--text-muted);
  }
  .confirm-challenge code {
    background: var(--surface-raised);
    padding: 0.05rem 0.3rem;
    border-radius: var(--radius-sm, 4px);
    color: var(--text-primary);
  }
  .confirm-challenge-input {
    width: 100%;
    box-sizing: border-box;
    background: var(--surface-raised);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm, 4px);
    padding: 0.35rem 0.5rem;
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-sm);
  }
</style>
