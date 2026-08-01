<script lang="ts">
  import type { Snippet } from 'svelte';
  import HintPopover from './HintPopover.svelte';

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
    /** Widen the control's cap from 24rem to 36rem. */
    wide?: boolean;
    /** Label beside the control rather than above it. */
    checkbox?: boolean;
    /**
     * Render a `<div>` rather than a `<label>`.
     *
     * Required whenever the slot holds a **button**. A `<button>` is a
     * labelable element, so inside a `<label>` it becomes the label's implicit
     * control and clicking the field's caption activates it — which for a
     * Stop button means the caption stops the thing. `HintPopover` dodges the
     * same hazard by rendering its trigger as `<span role="button">`, but that
     * only protects the "?" and not whatever the caller passes in.
     *
     * Also required for a row of several controls (a set of module
     * checkboxes), where one implicit label claims the first of them and
     * leaves the rest unlabelled.
     *
     * A `<label>` is still right, and still the default, for the ordinary case
     * of one input: clicking the caption should focus it.
     */
    labelled?: boolean;
    /**
     * Extra classes on the field element, for placement only — a grid span, a
     * column start. Not for restyling the field itself: that is what the props
     * are for, and a page reaching in to change the appearance is how the
     * forked copies of this component came about in the first place.
     */
    class?: string;
    children: Snippet;
  }

  let {
    label,
    hint,
    warning,
    error,
    wide = false,
    checkbox = false,
    labelled = true,
    class: className = '',
    children,
  }: Props = $props();
</script>

<!--
  Label + control + supplementary text, extracted from SettingsField so it is
  usable outside a settings page. SettingsField keeps its name and API and
  delegates here; this is also what deleted the byte-identical second copy of
  these rules that lived in lib/styles/settings.css.

  The descendant :global() rules below are what style an arbitrary control the
  caller passes in — a bare <input>, a Select, a SecretField. Input/TextArea
  bring their own identical appearance so they also work on their own.
-->
<!--
  `control-row` is the field tier's global scope (app.css). Composed here
  rather than restated, so a form row and a body toolbar are sized by one
  definition — including the touch floor, which is what keeps this field level
  with a `fullWidth` Select under it once iOS floors the input's text at 16px.
  It sets tokens and no layout, so it cannot disturb the arrangement below.
-->
<svelte:element
  this={labelled ? 'label' : 'div'}
  class="field control-row"
  class:field-wide={wide}
  class:checkbox
>
  {#if checkbox}
    {@render children()}
    <span class="field-label">{label}<HintPopover text={hint} label="About {label}" /></span>
  {:else}
    <span class="field-label">{label}<HintPopover text={hint} label="About {label}" /></span>
    {@render children()}
  {/if}
  {#if warning}<small class="field-warning">{warning}</small>{/if}
  {#if error}<small class="field-error">{error}</small>{/if}
</svelte:element>

<style>
  .field {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    font-size: var(--text-sm);
    /* A flex/grid item floors at its content width without this, so a field
       in a form grid can push the grid wider than its container. */
    min-width: 0;
  }

  .field-label {
    display: flex;
    align-items: center;
    gap: var(--space-1);
    color: var(--text-muted);
  }

  .field :global(input:not([type='checkbox'])),
  .field :global(select),
  .field :global(textarea) {
    background: var(--surface-base);
    color: var(--text-primary);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2);
    font: inherit;
    font-size: var(--text-sm);
    width: 100%;
    max-width: 24rem;
    min-width: 0;
    box-sizing: border-box;
  }

  /* `font: inherit` above is a shorthand and carries the body's 1.5 leading
     with it, at a specificity no weightless rule can correct — so an input
     here grew past the field tier it sits in the moment iOS floored its text
     at 16px. Pinned beside the shorthand rather than centrally for that
     reason. Textareas keep the inherited leading: they are sized by their
     rows and read as prose. */
  .field :global(input:not([type='checkbox'])),
  .field :global(select) {
    line-height: 1.2;
  }

  .field :global(textarea) {
    font-family: var(--font-mono);
    resize: vertical;
  }

  .field-wide :global(textarea),
  .field-wide :global(input) {
    max-width: 36rem;
  }

  .field.checkbox {
    flex-direction: row;
    align-items: center;
    gap: var(--space-2);
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
