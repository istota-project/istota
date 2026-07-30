<script lang="ts">
  import type { HTMLTextareaAttributes } from 'svelte/elements';

  interface Props extends Omit<HTMLTextareaAttributes, 'value'> {
    value?: string;
    invalid?: boolean;
    /** Off by default here, unlike Input: a textarea in this app is usually
     *  a config blob or a path list, which is where monospace earns its keep —
     *  but prose fields exist too, so it stays a choice. */
    monospace?: boolean;
  }

  let {
    value = $bindable(''),
    rows = 3,
    invalid = false,
    monospace = false,
    ...rest
  }: Props = $props();
</script>

<textarea
  {...rest}
  {rows}
  bind:value
  class="ui-textarea"
  class:mono={monospace}
  class:invalid
  aria-invalid={invalid || undefined}
></textarea>

<style>
  .ui-textarea {
    width: 100%;
    min-width: 0;
    box-sizing: border-box;
    background: var(--surface-base);
    color: var(--text-primary);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    padding: 0.35rem 0.5rem;
    font: inherit;
    font-size: var(--text-sm);
    /* Vertical only: a horizontally resizable textarea escapes its column and
       drags the rest of the form with it. */
    resize: vertical;
  }

  .ui-textarea:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .ui-textarea.mono {
    font-family: var(--font-mono);
  }

  .ui-textarea.invalid {
    border-color: var(--status-danger-fg);
  }
</style>
