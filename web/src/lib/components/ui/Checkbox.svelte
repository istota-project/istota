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
    /** The accessible name. Beside a visible `label` it replaces it for a
     *  screen reader, for a short label repeated down a list ("Excluded"). */
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
    // No fallback: binding a field that may be undefined (an unset profile
    // flag) to a bindable with a fallback is a Svelte runtime error.
    checked = $bindable(),
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
  A native checkbox, so focus and the keyboard stay the platform's, drawn by
  us so it is one size and one shape everywhere. Forced colours get the
  platform's box back.
-->
{#snippet box()}
  <input
    {...rest}
    bind:this={input}
    type="checkbox"
    class="ui-checkbox-input touch-target"
    checked={!!checked}
    {disabled}
    id={boxId}
    aria-label={ariaLabel}
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

  /* The fill and the mark take the primary button's pair. The mark is the
     ::after (the shared .touch-target overlay owns ::before). */
  .ui-checkbox-input {
    appearance: none;
    display: inline-grid;
    place-content: center;
    flex-shrink: 0;
    width: var(--checkbox-size);
    height: var(--checkbox-size);
    margin: 0;
    /* Off the baseline, which the mark's shape would otherwise move: the
       select-all dash grew the file table's header by two pixels. */
    vertical-align: middle;
    border: 1.5px solid var(--control-border-strong);
    border-radius: var(--radius-sm);
    background: var(--surface-base);
    cursor: pointer;
    transition:
      background-color var(--transition-fast),
      border-color var(--transition-fast);
  }

  .ui-checkbox-input::after {
    content: '';
    box-sizing: content-box;
    width: calc(var(--checkbox-size) * 0.22);
    height: calc(var(--checkbox-size) * 0.46);
    border: solid var(--surface-base);
    border-width: 0 2px 2px 0;
    transform: translateY(-12%) rotate(45deg) scale(0);
    transition: transform var(--transition-fast);
  }

  .ui-checkbox-input:hover:not(:disabled) {
    border-color: var(--text-secondary);
  }

  .ui-checkbox-input:is(:checked, :indeterminate) {
    border-color: var(--accent);
    background: var(--accent);
  }

  .ui-checkbox-input:is(:checked, :indeterminate):hover:not(:disabled) {
    border-color: var(--accent-hover);
    background: var(--accent-hover);
  }

  .ui-checkbox-input:checked::after {
    transform: translateY(-12%) rotate(45deg) scale(1);
  }

  .ui-checkbox-input:indeterminate::after {
    width: calc(var(--checkbox-size) * 0.5);
    height: 0;
    border-width: 0 0 2px;
    transform: none;
  }

  .ui-checkbox-input[aria-invalid='true'] {
    border-color: var(--status-danger-fg);
  }

  .ui-checkbox-input:disabled {
    cursor: not-allowed;
    opacity: var(--disabled-opacity);
  }

  /* Bare in a table cell, the box gets the comfortable size as its target. */
  @media (pointer: coarse) {
    .ui-checkbox-input {
      --touch-target-w: var(--touch-comfort);
      --touch-target-h: var(--touch-comfort);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .ui-checkbox-input,
    .ui-checkbox-input::after {
      transition: none;
    }
  }

  @media (forced-colors: active) {
    .ui-checkbox-input {
      appearance: auto;
    }

    .ui-checkbox-input::after {
      content: none;
    }
  }
</style>
