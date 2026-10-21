<svelte:options runes={true} />

<script lang="ts">
  import type { Snippet } from 'svelte';
  import Badge from './Badge.svelte';
  import HintPopover from './HintPopover.svelte';
  import { joinIds, setFieldContext, type FieldControlKind } from './fieldContext';

  interface Props {
    label: string;
    /** A short state beside the label, as a compact `Badge` — "Set by your
     *  administrator" on a field the deployment owns. Visible, unlike `hint`. */
    badge?: string;
    /** A small mark after the label, for a state that a word would crowd:
     *  the config editor's lock on a key that needs confirmation. */
    labelMark?: Snippet;
    /** Guidance shown in a popover behind a "?" beside the label. Optional
     *  reading — anything the user must see belongs in `description`,
     *  `warning` or `error`. */
    hint?: string;
    /** Visible help under the control, in the `.caption` role. */
    description?: string | Snippet;
    /** A condition the user needs to notice but which doesn't block saving —
     *  a stored value that no longer means what it says, say. Rendered inline
     *  precisely because a hover popover is discoverable, not seen. */
    warning?: string;
    error?: string;
    /**
     * `stacked` puts the label above the control. `inline` puts it in a
     * `--field-label-width` column beside the control from 641px up, and stacks
     * below that (the config editor). `compact` is the dense toolbar form: an
     * uppercase `.micro-label` caption, and the field grows from 9rem in a
     * wrapping row (the money rule and portfolio cards).
     */
    layout?: 'stacked' | 'inline' | 'compact';
    /** Sets `aria-required` on the control. No visible marker: most fields in
     *  the app are required, so the marker would be noise. */
    required?: boolean;
    /** Appends "(optional)" to the label. */
    optional?: boolean;
    /** Widen the control's cap from 24rem to 36rem. */
    wide?: boolean;
    /** Label beside the control rather than above it. */
    checkbox?: boolean;
    /** @deprecated No effect. The field is a `<div>` whatever it holds, and a
     *  control takes the label through the field context; goes next release. */
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
    badge,
    labelMark,
    hint,
    description,
    warning,
    error,
    layout = 'stacked',
    required = false,
    optional = false,
    wide = false,
    checkbox = false,
    class: className = '',
    children,
  }: Props = $props();

  const uid = $props.id();
  const controlId = `${uid}-control`;
  const labelId = `${uid}-label`;
  const descriptionId = `${uid}-description`;
  const warningId = `${uid}-warning`;
  const errorId = `${uid}-error`;

  const describedBy = $derived(
    joinIds(description ? descriptionId : null, warning ? warningId : null, error ? errorId : null),
  );

  let claimed = $state<{ kind: FieldControlKind; id?: () => string | null | undefined } | null>(
    null,
  );

  setFieldContext({
    controlId,
    labelId,
    get describedBy() {
      return describedBy;
    },
    get invalid() {
      return !!error;
    },
    get required() {
      return required;
    },
    claim(kind, id) {
      if (claimed) return false;
      claimed = { kind, id };
      return true;
    },
  });

  const native = $derived(claimed?.kind === 'native');
  const forId = $derived(claimed?.id?.() || controlId);
</script>

{#snippet labelText()}{label}{#if optional}{' '}<span class="field-optional">(optional)</span
    >{/if}<HintPopover text={hint} label="About {label}" />{#if badge}<Badge size="sm"
      >{badge}</Badge
    >{/if}{@render labelMark?.()}{/snippet}

{#snippet fieldLabel()}
  {#if native}
    <label class="field-label" class:micro-label={layout === 'compact'} id={labelId} for={forId}
      >{@render labelText()}</label
    >
  {:else}
    <span class="field-label" class:micro-label={layout === 'compact'} id={labelId}
      >{@render labelText()}</span
    >
  {/if}
{/snippet}

<!--
  Label + control + supplementary text.

  A <div>, never a <label> around the control. A <label> makes its first
  labelable descendant its control, so a Select trigger, a chip's remove button
  or a Stop button inside one became a second way to press it from the caption
  (it shipped as a "Tracking" caption that stopped tracking, and a "Sender
  filters" caption that deleted a sender). The control instead reads the field
  context: a text input, textarea or checkbox takes the id the caption's `for`
  names, so clicking the caption focuses it; anything else takes the caption by
  `aria-labelledby` and the caption is a plain <span>. A field holding no single
  control (a row of checkboxes, a chip list) is a group named by its caption.

  `control-row` is the field tier's global scope (app.css), composed here
  rather than restated, so a form row and a body toolbar are sized by one
  definition. It sets tokens and no layout.
-->
<div
  class="field control-row {className}"
  class:field-wide={wide}
  class:checkbox
  class:inline={layout === 'inline'}
  class:field-compact={layout === 'compact'}
  role={claimed ? undefined : 'group'}
  aria-labelledby={claimed ? undefined : labelId}
>
  {#if checkbox}
    {@render children()}
    {@render fieldLabel()}
  {:else}
    {@render fieldLabel()}
    {@render children()}
  {/if}
  {#if description}<small class="caption field-description" id={descriptionId}
      >{#if typeof description === 'string'}{description}{:else}{@render description()}{/if}</small
    >{/if}
  {#if warning}<small class="field-warning" id={warningId}>{warning}</small>{/if}
  {#if error}<small class="field-error" id={errorId}>{error}</small>{/if}
</div>

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
  }

  .field-label:not(.micro-label) {
    color: var(--text-muted);
  }

  .field-optional {
    color: var(--text-dim);
  }

  /* A form input reads at a line's length rather than the card's width. The
     cap is the field's, so it reaches the primitives by their own classes;
     inside :where() it weighs no more than the field's class, and a
     placement class on the control outranks it. Field styles nothing else
     about a control: every control in a field is a primitive that draws
     itself (fieldCascade.test.ts). */
  .field :global(:where(.ui-input, .ui-input-box, .ui-textarea)) {
    max-width: 24rem;
  }

  .field-wide :global(:where(.ui-input, .ui-input-box, .ui-textarea)) {
    max-width: 36rem;
  }

  /* Control in the first column, caption and everything under it in the
     second, so a description or an error sits under the caption it explains. */
  .field.checkbox {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    column-gap: var(--space-2);
    color: var(--text-primary);
  }

  .field.checkbox > :global(*) {
    grid-column: 2;
  }

  .field.checkbox > :global(:first-child) {
    grid-column: 1;
  }

  .field.checkbox .field-label {
    color: var(--text-primary);
  }

  /* The money `.ctl`: a dense field that grows from 9rem in a wrapping row. */
  .field-compact {
    gap: var(--space-1);
    flex: 1 1 9rem;
  }

  /* The row sizes a compact field, so its control fills it uncapped. */
  .field-compact :global(:where(.ui-input, .ui-input-box, .ui-textarea)) {
    max-width: none;
  }

  .field-description {
    max-width: 24rem;
  }

  .field-warning {
    font-size: var(--text-xs);
    color: var(--status-warn-fg);
    /* Wrap at the input width so a long warning forms a tidy column under the
		   field instead of stretching the whole container. */
    max-width: 24rem;
  }

  .field-wide :is(.field-warning, .field-description) {
    max-width: 36rem;
  }

  /* Placed explicitly rather than by source order: a checkbox field renders
     its control before its label. */
  @media (min-width: 641px) {
    .field.inline {
      display: grid;
      grid-template-columns: var(--field-label-width) minmax(0, 1fr);
      /* A control's height even for a checkbox, so the rows of a form match. */
      grid-template-rows: minmax(var(--control-height-lg), auto);
      column-gap: var(--space-3);
      row-gap: var(--space-1);
      /* Top, not centre: a label that wraps or carries a badge grows the row,
         and centring would push the control down out of line with anything
         beside the field (the config editor's Undo and Reset). */
      align-items: start;
    }

    .field.inline > :global(*),
    .field.inline.checkbox > :global(:first-child) {
      grid-column: 2;
    }

    /* A config key has no spaces to break at, so a long one breaks anywhere
       rather than pushing the control column out of line. The control's
       height as a floor centres a one-line label on the control. */
    .field.inline > .field-label {
      grid-column: 1;
      grid-row: 1;
      min-height: var(--control-height-lg);
      flex-wrap: wrap;
      overflow-wrap: anywhere;
      color: var(--text-muted);
    }

    /* Centred in the control-height band the top-aligned row gives it. */
    .field.inline :global(input[type='checkbox']) {
      justify-self: start;
      margin-top: calc((var(--control-height-lg) - var(--checkbox-size)) / 2);
    }
  }

  .field-error {
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
  }
</style>
