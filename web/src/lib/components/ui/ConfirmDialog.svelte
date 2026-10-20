<script lang="ts">
  import type { Snippet } from 'svelte';
  import Modal from './Modal.svelte';
  import Button from './Button.svelte';
  import Input from './Input.svelte';

  /**
   * The answer to a confirm or secondary action. A promise is awaited with the
   * confirm button in its loading state and dismissal held; returning (or
   * resolving) closes the dialog, resolving `false` keeps it open with no
   * message, and a throw or rejection keeps it open with the message as a
   * `.form-error`.
   */
  type Action = () => unknown;

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
    /** The confirm button's label while its action is pending. */
    busyLabel?: string;
    cancelLabel?: string;
    /** Confirm-button style. "danger" (red outline) for destructive actions. */
    confirmVariant?: 'danger' | 'primary';
    /**
     * Type-to-confirm challenge. When set, the user must type this exact string
     * (e.g. a room name) before the confirm button enables — the destructive
     * high-friction path (RoomSettings hard delete).
     */
    challenge?: string;
    /** "Not yet allowed", layered on the challenge gate. Not for progress: an async `onConfirm` is. */
    confirmDisabled?: boolean;
    onConfirm: Action;
    onCancel?: () => void;
    /** A third choice between Cancel and the confirm (Disconnect, keep sharing). */
    secondaryLabel?: string;
    secondaryBusyLabel?: string;
    onSecondary?: Action;
    /** A form-level error the caller holds; a rejected action's message wins. */
    error?: string;
    width?: string;
  }

  let {
    open = $bindable(false),
    title,
    message,
    body,
    confirmLabel = 'Delete',
    busyLabel = 'Working…',
    cancelLabel = 'Cancel',
    confirmVariant = 'danger',
    challenge,
    confirmDisabled = false,
    onConfirm,
    onCancel,
    secondaryLabel,
    secondaryBusyLabel = 'Working…',
    onSecondary,
    error = '',
    width = '380px',
  }: Props = $props();

  let typed = $state('');
  let running = $state<'confirm' | 'secondary' | null>(null);
  let failure = $state('');

  // Reset the challenge field and the last failure whenever the dialog opens.
  $effect(() => {
    if (open) {
      typed = '';
      failure = '';
    }
  });

  const challengeMet = $derived(challenge == null || typed === challenge);
  const canConfirm = $derived(challengeMet && !confirmDisabled);
  const pending = $derived(running !== null);
  const shownError = $derived(failure || error);

  async function run(which: 'confirm' | 'secondary', action: Action) {
    if (pending) return;
    failure = '';
    running = which;
    try {
      const result = await action();
      if (result !== false) open = false;
    } catch (e) {
      failure = e instanceof Error ? e.message : String(e);
    } finally {
      running = null;
    }
  }

  function cancel() {
    if (pending) return;
    open = false;
    onCancel?.();
  }

  function confirm() {
    if (!canConfirm) return;
    void run('confirm', onConfirm);
  }

  function handleOpenChange(next: boolean) {
    if (!next) onCancel?.();
  }
</script>

<Modal bind:open {title} {width} busy={pending} onOpenChange={handleOpenChange} onsubmit={confirm}>
  {#if body}
    {@render body()}
  {:else if message}
    <p class="confirm-message">{message}</p>
  {/if}

  {#if challenge != null}
    <p class="confirm-challenge description">
      Type <code>{challenge}</code> to confirm.
    </p>
    <Input
      bind:value={typed}
      placeholder={challenge}
      autocomplete="off"
      aria-label="Type to confirm"
    />
  {/if}

  {#if shownError}<p class="form-error confirm-error" role="alert">{shownError}</p>{/if}

  {#snippet footer()}
    <Button variant="secondary" onclick={cancel} disabled={pending}>{cancelLabel}</Button>
    {#if secondaryLabel && onSecondary}
      <Button
        variant="secondary"
        onclick={() => void run('secondary', onSecondary)}
        loading={running === 'secondary'}
        loadingLabel={secondaryBusyLabel}
        disabled={pending}>{secondaryLabel}</Button
      >
    {/if}
    <Button
      type="submit"
      variant={confirmVariant}
      loading={running === 'confirm'}
      loadingLabel={busyLabel}
      disabled={!canConfirm || pending}>{confirmLabel}</Button
    >
  {/snippet}
</Modal>

<style>
  .confirm-message {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-primary);
    line-height: var(--leading-normal);
  }
  .confirm-challenge {
    margin: var(--space-3) 0 var(--space-2);
  }
  .confirm-challenge code {
    background: var(--surface-raised);
    padding: 0.05rem var(--space-1);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
  }
  .confirm-error {
    margin: var(--space-3) 0 0;
  }
</style>
