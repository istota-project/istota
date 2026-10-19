<script lang="ts">
  import { untrack, type Snippet } from 'svelte';
  import type { HTMLInputAttributes } from 'svelte/elements';
  import { getFieldContext, joinIds } from './fieldContext';

  interface Props extends Omit<
    HTMLInputAttributes,
    'type' | 'checked' | 'onchange' | 'class' | 'children'
  > {
    checked?: boolean;
    /** The mixed state of a select-all box over a partial selection. */
    indeterminate?: boolean;
    /** Omit it in a table cell or inside a `Field`; outside a field, pass
     *  `ariaLabel` instead. */
    label?: string | Snippet;
    ariaLabel?: string;
    /** A visible line under the label. */
    description?: string;
    disabled?: boolean;
    /** `muted` for a toolbar toggle ("Folders first", "Show hidden"). */
    tone?: 'default' | 'muted';
    onchange?: (checked: boolean) => void;
    /** Placement only. */
    class?: string;
  }

  let {
    checked = $bindable(false),
    indeterminate = false,
    label,
    ariaLabel,
    description,
    disabled = false,
    tone = 'default',
    onchange,
    class: className = '',
    id,
    'aria-describedby': ariaDescribedBy,
    ...rest
  }: Props = $props();

  // With no label of its own, a Field's caption names it through `for`.
  const field = untrack(() => label) ? null : getFieldContext();
  const owned = field?.claim('native', () => id) ?? false;

  if (import.meta.env.DEV) {
    untrack(() => {
      if (!label && !ariaLabel && !field && !rest['aria-labelledby']) {
        console.warn('Checkbox: pass `label`, or `ariaLabel` where no label is drawn');
      }
    });
  }

  const uid = $props.id();
  const descriptionId = `${uid}-description`;
  // A labelled box needs an id for its <label for>; a bare one only inside a field.
  const boxId = $derived(id ?? (owned ? field?.controlId : label ? `${uid}-box` : undefined));

  let input = $state<HTMLInputElement | null>(null);

  // `indeterminate` is a DOM property with no attribute.
  $effect(() => {
    if (input) input.indeterminate = indeterminate;
  });

  function change(e: Event & { currentTarget: HTMLInputElement }) {
    checked = e.currentTarget.checked;
    onchange?.(checked);
  }
</script>

<!--
  A native checkbox, so focus, forced colours and the keyboard stay the
  platform's. The fill is `--accent`, which the file table and the files page
  already used; everything else was the user agent's blue.
-->
{#snippet box()}
  <input
    {...rest}
    bind:this={input}
    type="checkbox"
    class="ui-checkbox-input"
    {checked}
    {disabled}
    id={boxId}
    aria-label={label ? undefined : ariaLabel}
    aria-describedby={joinIds(
      ariaDescribedBy,
      description ? descriptionId : undefined,
      owned ? field?.describedBy : undefined,
    )}
    aria-invalid={(owned && field?.invalid) || undefined}
    onchange={change}
  />
{/snippet}

{#if label}
  <span class="ui-checkbox {className}" class:muted={tone === 'muted'} class:disabled>
    {@render box()}
    <label class="ui-checkbox-label" for={boxId}
      >{#if typeof label === 'string'}{label}{:else}{@render label()}{/if}</label
    >
    {#if description}<span class="caption ui-checkbox-description" id={descriptionId}
        >{description}</span
      >{/if}
  </span>
{:else}
  {@render box()}
{/if}

<style>
  /* The box in the first column; the label, and the description under it, in
     the second. The description sits outside the <label> so it does not join
     the box's name. */
  .ui-checkbox {
    display: inline-grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    column-gap: var(--space-2);
    row-gap: var(--space-1);
    font-size: var(--text-sm);
    color: var(--text-primary);
  }

  .ui-checkbox.muted {
    color: var(--text-muted);
  }

  .ui-checkbox-label {
    cursor: pointer;
  }

  .ui-checkbox.disabled .ui-checkbox-label {
    cursor: not-allowed;
  }

  .ui-checkbox-description {
    grid-column: 2;
  }

  .ui-checkbox-input {
    accent-color: var(--accent);
    flex-shrink: 0;
  }

  .ui-checkbox-input:disabled {
    cursor: not-allowed;
  }
</style>
