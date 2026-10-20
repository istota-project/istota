<script lang="ts">
  import type { Snippet } from 'svelte';
  import Modal from './Modal.svelte';
  import Button from './Button.svelte';

  /**
   * A dialog that edits something and saves it: `Modal` in form mode with the
   * Cancel / primary footer, the form error and the in-flight state.
   *
   * `onSubmit` decides when it closes. It closes once the handler returns or
   * its promise resolves; a throw or a rejection keeps it open with the
   * message as the form error and the draft untouched; resolving `false` keeps
   * it open with no message, for a refusal the form reports beside a field.
   * While the submit is pending (or `busy` says the caller is), the submit
   * button shows `busyLabel`, Cancel is disabled and Escape and an outside
   * click do nothing.
   */
  interface Props {
    open?: boolean;
    title: string;
    description?: string;
    width?: string;
    /** A form-level error the caller holds (a failed load); a rejected submit's message wins. */
    error?: string;
    busy?: boolean;
    submitLabel?: string;
    busyLabel?: string;
    submitVariant?: 'primary' | 'danger';
    canSubmit?: boolean;
    cancelLabel?: string;
    onSubmit: () => void | boolean | Promise<void | boolean>;
    onCancel: () => void;
    /** Buttons left of Cancel (a Delete). Give them no `type="submit"`. */
    extraActions?: Snippet;
    onOpenAutoFocus?: (event: Event) => void;
    children: Snippet;
  }

  let {
    open = $bindable(false),
    title,
    description,
    width,
    error = '',
    busy = false,
    submitLabel = 'Save',
    busyLabel = 'Saving…',
    submitVariant = 'primary',
    canSubmit = true,
    cancelLabel = 'Cancel',
    onSubmit,
    onCancel,
    extraActions,
    onOpenAutoFocus,
    children,
  }: Props = $props();

  let submitting = $state(false);
  let failure = $state('');
  const pending = $derived(busy || submitting);
  const shownError = $derived(failure || error);

  // A dialog kept mounted between uses must not reopen on the last refusal.
  $effect(() => {
    if (open) failure = '';
  });

  async function submit() {
    if (pending || !canSubmit) return;
    failure = '';
    submitting = true;
    try {
      const result = await onSubmit();
      if (result !== false) open = false;
    } catch (e) {
      failure = e instanceof Error ? e.message : String(e);
    } finally {
      submitting = false;
    }
  }

  function cancel() {
    if (pending) return;
    open = false;
    onCancel();
  }

  function handleOpenChange(next: boolean) {
    if (!next) onCancel();
  }
</script>

<Modal
  bind:open
  {title}
  {description}
  {width}
  {onOpenAutoFocus}
  busy={pending}
  onsubmit={submit}
  onOpenChange={handleOpenChange}
>
  {@render children()}
  {#if shownError}<p class="form-error ui-form-modal-error" role="alert">{shownError}</p>{/if}
  {#snippet footer()}
    {@render extraActions?.()}
    <Button variant="secondary" onclick={cancel} disabled={pending}>{cancelLabel}</Button>
    <Button
      type="submit"
      variant={submitVariant}
      loading={pending}
      loadingLabel={busyLabel}
      disabled={!canSubmit}>{submitLabel}</Button
    >
  {/snippet}
</Modal>

<style>
  .ui-form-modal-error {
    margin: var(--space-3) 0 0;
  }
</style>
