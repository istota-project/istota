<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    /** The picked file, bindable. `null` shows the prompt. */
    file: File | null;
    /** `accept` for the underlying picker. */
    accept?: string;
    /** Runs after the file is cleared — for whatever the page derived from it. */
    onClear?: () => void;
    /** The empty-state prose. The only part that differed between the three
     *  pages this replaces. */
    children: Snippet;
  }

  let {
    file = $bindable(null),
    accept = 'image/*,application/pdf',
    onClear,
    children,
  }: Props = $props();

  let fileInput: HTMLInputElement | undefined = $state();

  function pickFile(e: Event) {
    file = (e.target as HTMLInputElement).files?.[0] ?? null;
  }

  function handleDrop(e: DragEvent) {
    e.preventDefault();
    const f = e.dataTransfer?.files?.[0];
    if (f) file = f;
  }

  // Image paste only. A page that also takes pasted *text* (the immunization
  // list) handles that on its own textarea, so this must not swallow the event
  // when the clipboard carries no file.
  function handlePaste(e: ClipboardEvent) {
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
  to drop.
-->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="dropzone"
  ondragover={(e) => e.preventDefault()}
  ondrop={handleDrop}
  onpaste={handlePaste}
  role="presentation"
>
  {#if file}
    <div class="picked">
      {file.name}
      <span class="hint">({Math.round(file.size / 1024)} KB)</span>
      <button type="button" class="clear" onclick={clearFile} aria-label="Clear selected file"
        >×</button
      >
    </div>
  {:else}
    {@render children()}
  {/if}
  <!-- Hidden once a file is picked: the .picked row above is the affordance
       then, and a second control offering "choose file" beside it reads as a
       different action. -->
  <input
    bind:this={fileInput}
    type="file"
    {accept}
    onchange={pickFile}
    class:hidden={file !== null}
  />
</div>

<style>
  .dropzone {
    border: 2px dashed var(--border-default);
    border-radius: var(--radius-card);
    padding: 1.5rem;
    text-align: center;
    color: var(--text-muted);
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .picked {
    color: var(--text-primary);
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
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

  .dropzone :global(.hint) {
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  input[type='file'] {
    align-self: center;
    font-size: var(--text-sm);
    color: var(--text-muted);
  }

  input[type='file'].hidden {
    display: none;
  }
</style>
