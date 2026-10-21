<svelte:options runes={true} />

<script lang="ts">
  import type { HTMLTextareaAttributes } from 'svelte/elements';
  import { getFieldContext, joinIds } from './fieldContext';

  interface Props extends Omit<HTMLTextareaAttributes, 'value'> {
    value?: string | null;
    invalid?: boolean;
    /** Off by default here, unlike Input: a textarea in this app is usually
     *  a config blob or a path list, which is where monospace earns its keep —
     *  but prose fields exist too, so it stays a choice. */
    monospace?: boolean;
    /** The table-cell editor: a tighter box for a grid of inputs. */
    dense?: boolean;
    /** The textarea element, bindable, for a caller that focuses or measures it. */
    ref?: HTMLTextAreaElement;
  }

  let {
    value = $bindable(''),
    rows = 3,
    invalid = false,
    monospace = false,
    dense = false,
    ref = $bindable(),
    id,
    class: className = '',
    'aria-describedby': ariaDescribedBy,
    ...rest
  }: Props = $props();

  // Inside a Field the caption's `for` names this textarea.
  const field = getFieldContext();
  const owned = field?.claim('native', () => id) ?? false;
</script>

<textarea
  {...rest}
  {rows}
  bind:value
  bind:this={ref}
  class="ui-textarea {className}"
  class:mono={monospace}
  class:invalid
  class:dense
  id={id ?? (owned ? field?.controlId : undefined)}
  aria-invalid={invalid || (owned && field?.invalid) || undefined}
  aria-describedby={joinIds(ariaDescribedBy, owned ? field?.describedBy : undefined)}
  aria-required={(owned && field?.required) || undefined}
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
    padding: var(--space-2);
    font: inherit;
    font-size: var(--text-sm);
    /* Vertical only: a horizontally resizable textarea escapes its column and
       drags the rest of the form with it. */
    resize: vertical;
  }

  .ui-textarea:disabled {
    opacity: var(--disabled-opacity);
    cursor: not-allowed;
  }

  .ui-textarea.mono {
    font-family: var(--font-mono);
  }

  .ui-textarea.invalid {
    border-color: var(--status-danger-fg);
  }

  .ui-textarea.dense {
    padding: var(--space-1);
    font-size: var(--text-xs);
  }
</style>
