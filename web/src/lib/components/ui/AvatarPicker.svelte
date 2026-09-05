<script lang="ts">
  import type { Snippet } from 'svelte';
  import { Camera } from 'lucide-svelte';
  import Button from './Button.svelte';

  interface Props {
    /**
     * The picture. A snippet rather than props, because the two call sites
     * render two different identities — `kind="user"` with an id and a hash off
     * the profile, `kind="bot"` with neither — and threading both shapes
     * through here would put the identity model in a control that only has to
     * show whatever it is given.
     */
    preview: Snippet;
    /**
     * Accessible name for the picture-as-button. It is the only name that
     * control has: the picture inside it is `alt`-labelled for what it shows,
     * which is the current state rather than the action.
     */
    pickLabel: string;
    /** The one-line prompt beside the picture. */
    prompt: string;
    /** `accept` for the underlying picker. */
    accept?: string;
    /**
     * What is happening, while it is. Non-empty replaces the prompt *and*
     * unmounts the picker — see the comment on the template below, where both
     * halves of that are load-bearing.
     */
    busyLabel?: string;
    /** Whether there is a picture to remove. Gates the Remove control. */
    removable?: boolean;
    /** A picked file, from any of the three routes. */
    onPick: (file: File) => void;
    onRemove?: () => void;
  }

  let {
    preview,
    pickLabel,
    prompt,
    accept = 'image/*',
    busyLabel = '',
    removable = false,
    onPick,
    onRemove,
  }: Props = $props();

  let fileInput: HTMLInputElement | undefined = $state();
  let dragging = $state(false);

  /* Every route in is gated on `busyLabel`, which is what lets the caller take
     the file straight through `onPick` instead of staging it. Drop and paste
     reach the wrapper, which stays mounted while the picker does not, so
     without the guard a second file could be sent mid-upload — two concurrent
     PUTs resolving in either order, leaving the preview on one hash and the
     stored row on the other. */
  function take(file: File | null | undefined) {
    if (busyLabel || !file) return;
    onPick(file);
  }

  function handleDrop(e: DragEvent) {
    e.preventDefault();
    dragging = false;
    take(e.dataTransfer?.files?.[0]);
  }

  function handleDragOver(e: DragEvent) {
    e.preventDefault();
    if (!busyLabel) dragging = true;
  }

  // Image paste only, and only when the clipboard actually carries a file —
  // this sits inside pages with text fields of their own, and must not swallow
  // an ordinary paste.
  function handlePaste(e: ClipboardEvent) {
    const f = e.clipboardData?.files?.[0];
    if (f) {
      e.preventDefault();
      take(f);
    }
  }
</script>

<!--
  Pick or remove one identity's picture.

  The picture *is* the picker. What this replaced was a 4rem preview sitting
  beside a full-width dashed dropzone with a "Choose file" button inside it and
  a "Remove" button under it — three targets and about 230px of card for a
  control that is used once. Here the preview takes the click, the drop and the
  paste, and the only remaining button is the one that does the other thing.

  Both pages carried that arrangement verbatim, down to the CSS and the
  comments; the copies had already drifted in their prose. This is the one copy.
-->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="avatar-picker"
  class:dragging
  ondragover={handleDragOver}
  ondragleave={() => (dragging = false)}
  ondrop={handleDrop}
  onpaste={handlePaste}
  role="presentation"
>
  <!--
    The picker is unmounted while busy rather than disabled, and that is
    load-bearing: remounting is what resets the native input, and a browser
    fires no `change` for a file the input is already holding — so re-picking
    the photo whose upload just failed would otherwise do nothing and say
    nothing. Replacing this with a `disabled` state has to restore that reset
    some other way. The preview stays put across both branches, so the picture
    does not move when an upload starts.
  -->
  {#if busyLabel}
    <span class="target is-busy">{@render preview()}</span>
  {:else}
    <button type="button" class="target" aria-label={pickLabel} onclick={() => fileInput?.click()}>
      {@render preview()}
      <span class="overlay" aria-hidden="true"><Camera size={18} /></span>
    </button>
    <!-- `hidden` rather than a class, so the input leaves the a11y tree with
         it: the picture is the affordance, and .click() reaches it either way.
         A sibling of the button rather than a child, which is not valid. -->
    <input
      bind:this={fileInput}
      type="file"
      {accept}
      hidden
      onchange={(e) => take((e.currentTarget as HTMLInputElement).files?.[0])}
    />
  {/if}

  <div class="side">
    {#if busyLabel}
      <p class="hint busy" aria-live="polite">{busyLabel}</p>
    {:else}
      <p class="hint">{prompt}</p>
      {#if removable}
        <Button variant="ghost" size="sm" onclick={onRemove}>Remove</Button>
      {/if}
    {/if}
  </div>
</div>

<style>
  .avatar-picker {
    display: flex;
    align-items: center;
    gap: var(--space-4);
  }

  /* The size goes on the wrapper rather than on the Avatar primitive, which
     reads it and holds no opinion about how big an identity is. */
  .target {
    flex: 0 0 auto;
    --avatar-size: 4rem;
    position: relative;
    display: block;
    padding: 0;
    background: none;
    border: none;
    border-radius: var(--radius-md);
    cursor: pointer;
    /* The dashed ring the old dropzone carried, moved onto the thing being
       dropped on. `outline` rather than a border, so it does not resize the
       picture underneath it when it appears. */
    outline: 2px dashed transparent;
    outline-offset: 3px;
    transition: outline-color 0.12s ease;
  }

  .target.is-busy {
    cursor: default;
    opacity: 0.6;
  }

  /* The drag ring is on the wrapper, not the button: the drop target is the
     whole control, and a ring that only appears once the cursor is over the
     64px picture is a ring nobody sees in time. */
  .avatar-picker.dragging .target,
  .target:hover,
  .target:focus-visible {
    outline-color: var(--border-hover);
  }

  /* The affordance. Withheld until hover or keyboard focus — at rest the
     picture is the state, and a permanent badge over a face reads as part of
     it. `:focus-visible` rather than `:focus` so a mouse click does not leave
     it stuck on afterwards. */
  .overlay {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: var(--radius-md);
    background: var(--scrim-bg);
    color: var(--on-scrim-fg);
    opacity: 0;
    transition: opacity 0.12s ease;
  }

  .avatar-picker.dragging .overlay,
  .target:hover .overlay,
  .target:focus-visible .overlay {
    opacity: 1;
  }

  .side {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-1);
  }

  .side .hint {
    margin: 0;
  }

  /* `ghost` rather than the `secondary` this replaced: Remove is the lesser of
     two actions here and the picture is the other one, so a filled box of its
     own is what made the old arrangement read as a row of equals. Pulled back
     by its own inline padding so its label starts on the prompt's left edge
     rather than indented under it. */
  .side :global(.btn) {
    margin-inline-start: calc(-1 * var(--space-2));
  }

  @media (prefers-reduced-motion: reduce) {
    .target,
    .overlay {
      transition: none;
    }
  }
</style>
