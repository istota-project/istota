<script lang="ts">
  import type { Snippet } from 'svelte';
  import HintPopover from '../ui/HintPopover.svelte';

  interface Props {
    label: string;
    /** Guidance shown in a popover behind a "?" beside the label. Optional
     *  reading — anything the user must see belongs in `warning` or `error`. */
    hint?: string;
    /** A condition the user needs to notice but which doesn't block saving —
     *  a stored value that no longer means what it says, say. Rendered inline
     *  precisely because a hover popover is discoverable, not seen. */
    warning?: string;
    error?: string;
    wide?: boolean;
    checkbox?: boolean;
    children: Snippet;
  }

  let { label, hint, warning, error, wide = false, checkbox = false, children }: Props = $props();
</script>

<label class="field" class:field-wide={wide} class:checkbox>
  {#if checkbox}
    {@render children()}
    <span class="field-label">{label}<HintPopover text={hint} label="About {label}" /></span>
  {:else}
    <span class="field-label">{label}<HintPopover text={hint} label="About {label}" /></span>
    {@render children()}
  {/if}
  {#if warning}<small class="field-warning">{warning}</small>{/if}
  {#if error}<small class="field-error">{error}</small>{/if}
</label>

<style>
  .field {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    font-size: var(--text-sm);
  }

  .field-label {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    color: var(--text-muted);
  }

  .field :global(input:not([type='checkbox'])),
  .field :global(select),
  .field :global(textarea) {
    background: var(--surface-base);
    color: var(--text-primary);
    border: 1px solid var(--border-default);
    border-radius: 0.3rem;
    padding: 0.3rem 0.5rem;
    font: inherit;
    font-size: var(--text-sm);
    width: 100%;
    max-width: 24rem;
    min-width: 0;
    box-sizing: border-box;
  }

  .field :global(textarea) {
    font-family: var(--font-mono, ui-monospace, SFMono-Regular, monospace);
    resize: vertical;
  }

  .field-wide :global(textarea),
  .field-wide :global(input) {
    max-width: 36rem;
  }

  .field :global(input:focus),
  .field :global(select:focus),
  .field :global(textarea:focus) {
    outline: 1px solid var(--accent, var(--accent-blue));
  }

  .field.checkbox {
    flex-direction: row;
    align-items: center;
    gap: 0.4rem;
    color: var(--text-primary);
  }

  .field.checkbox .field-label {
    color: var(--text-primary);
  }

  .field.checkbox :global(input[type='checkbox']) {
    width: auto;
  }

  .field-warning {
    font-size: var(--text-xs);
    color: var(--status-warn-fg);
    /* Wrap at the input width so a long warning forms a tidy column under the
		   field instead of stretching the whole container. */
    max-width: 24rem;
  }

  .field-wide .field-warning {
    max-width: 36rem;
  }

  .field-error {
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
  }
</style>
