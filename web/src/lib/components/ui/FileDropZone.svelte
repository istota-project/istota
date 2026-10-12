<script lang="ts">
  import type { Snippet } from 'svelte';
  import Button from './Button.svelte';

  interface Props {
    /** The picked file, bindable. `null` shows the prompt. */
    file?: File | null;
    overlay?: boolean;
    disabled?: boolean;
    onFiles?: (files: File[]) => void;
    /** `accept` for the underlying picker. */
    accept?: string;
    /** Runs after the file is cleared — for whatever the page derived from it. */
    onClear?: () => void;
    /** The empty-state prose. The only part that differs between call sites. */
    children: Snippet;
  }

  let {
    file = $bindable(null),
    accept = 'image/*,application/pdf',
    onClear,
    overlay = false,
    disabled = false,
    onFiles,
    children,
  }: Props = $props();

  let fileInput: HTMLInputElement | undefined = $state();

  function pickFile(e: Event) {
    file = (e.target as HTMLInputElement).files?.[0] ?? null;
  }

  let dragDepth = $state(0);
  function handleDrop(e: DragEvent) {
    e.preventDefault();
    dragDepth = 0;
    if (disabled) return;
    const files = Array.from(e.dataTransfer?.files ?? []);
    if (onFiles) onFiles(files);
    else if (files[0]) file = files[0];
  }
  function dragEnter(e: DragEvent) {
    if (!disabled && e.dataTransfer?.types.includes('Files')) dragDepth++;
  }

  // Image paste only. A page that also takes pasted *text* (the immunization
  // list) handles that on its own textarea, so this must not swallow the event
  // when the clipboard carries no file.
  function handlePaste(e: ClipboardEvent) {
    if (disabled) return;
    if (onFiles && e.clipboardData?.files.length) {
      e.preventDefault();
      onFiles(Array.from(e.clipboardData.files));
      return;
    }
    const f = e.clipboardData?.files?.[0];
    if (f) {
      e.preventDefault();
      file = f;
    }
  }

  function clearFile() {
    file = null;
    // Resetting the input is what lets the same file be picked twice running;
    // without it the change event never fires the second time.
    if (fileInput) fileInput.value = '';
    onClear?.();
  }
</script>

<!--
  Drop / paste / pick, with the picked file and a clear button. The three
  health import screens each carried this verbatim — the same handlers, the
  same markup, the same CSS — differing only in the sentence describing what
  to drop. It lived under `components/health/` until money's portfolio import
  wanted the same control and had to reach across modules for it, which is the
  line between a module block and a primitive.
-->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="dropzone"
  class:overlay
  ondragenter={dragEnter}
  ondragleave={() => (dragDepth = Math.max(0, dragDepth - 1))}
  ondragover={(e) => e.preventDefault()}
  ondrop={handleDrop}
  onpaste={handlePaste}
  role="presentation"
>
  {#if overlay}
    {@render children()}
    {#if dragDepth > 0 && !disabled}<div class="drop-overlay">Drop files to upload</div>{/if}
  {:else if file}
    <div class="picked">
      {file.name}
      <span class="caption">({Math.round(file.size / 1024)} KB)</span>
      <button type="button" class="clear" onclick={clearFile} aria-label="Clear selected file"
        >×</button
      >
    </div>
  {:else}
    {@render children()}
    <!-- Withheld once a file is picked: the .picked row above is the
         affordance then, and a second control offering "choose file" beside it
         reads as a different action. -->
    <div class="pick">
      <Button variant="secondary" size="sm" onclick={() => fileInput?.click()}>Choose file</Button>
    </div>
  {/if}
  <!-- The picker is a button rather than the bare control, whose platform
       rendering ("Choose File — no file selected") is as wide as its longest
       state and cannot be narrowed. Centred in this column it overflowed both
       sides on a phone, putting the button outside the dashed border. `hidden`
       rather than a class, so the input leaves the a11y tree with it: the
       button is the affordance, and .click() reaches it either way. -->
  {#if !overlay}<input bind:this={fileInput} type="file" {accept} onchange={pickFile} hidden />{/if}
</div>

<style>
  .dropzone.overlay {
    position: relative;
    flex: 1;
    min-height: 0;
    padding: 0;
    border: 0;
    border-radius: 0;
    text-align: initial;
    gap: 0;
  }
  .drop-overlay {
    position: absolute;
    inset: var(--space-2);
    display: grid;
    place-items: center;
    border: 2px dashed var(--accent);
    border-radius: var(--radius-card);
    background: var(--surface-overlay);
    color: var(--text-primary);
    pointer-events: none;
  }

  .dropzone {
    border: 2px dashed var(--border-default);
    border-radius: var(--radius-card);
    padding: var(--space-6);
    text-align: center;
    color: var(--text-muted);
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .picked {
    color: var(--text-primary);
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    justify-content: center;
  }

  .clear {
    background: none;
    border: 1px solid var(--border-default);
    border-radius: 50%;
    color: var(--text-muted);
    width: 1.4rem;
    height: 1.4rem;
    line-height: 1;
    cursor: pointer;
    font-size: var(--text-sm);
  }

  .clear:hover {
    background: var(--surface-raised);
    color: var(--text-primary);
  }

  /* A row of its own rather than `align-self`, so a long prompt sentence above
     it wraps against the zone's width and the button stays centred under it. */
  .pick {
    display: flex;
    justify-content: center;
  }
</style>
