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
  let root = $state<HTMLElement | null>(null);

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

  // A raw <input>, <select> or <textarea> written straight into the slot reads
  // no context, and since the field is no longer a <label> nothing would name
  // it. When no primitive has claimed the field and the slot holds exactly one
  // such control, it gets the field's id and describedby here. Raw controls
  // inside a Field are what the Input migration removes. A raw control that
  // only appears after mount is not picked up.
  let rawControl: HTMLElement | null = null;
  $effect(() => {
    const ids = describedBy;
    const isRequired = required;
    if (!root) return;
    if (!rawControl) {
      if (claimed) return;
      const raw = root.querySelectorAll<HTMLElement>(
        'input:not([type="hidden"]), select, textarea',
      );
      if (raw.length !== 1) return;
      rawControl = raw[0];
      if (!rawControl.id) rawControl.id = controlId;
      const rawId = rawControl.id;
      claimed = { kind: 'native', id: () => rawId };
    }
    if (ids) rawControl.setAttribute('aria-describedby', ids);
    else rawControl.removeAttribute('aria-describedby');
    if (isRequired) rawControl.setAttribute('aria-required', 'true');
    else rawControl.removeAttribute('aria-required');
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
  bind:this={root}
  class="field control-row {className}"
  class:field-wide={wide}
  class:checkbox
  class:inline={layout === 'inline'}
  class:compact={layout === 'compact'}
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

  /* The descendant rules below are inside :where(), so they weigh no more than
     the field's own class: any class on the control outranks them, which is
     how Input's `monospace` and `invalid` stay visible inside a field. They
     exist for the raw controls still written inside a Field; the width cap is
     the only part that reaches the Input and TextArea primitives too. */
  .field :global(:where(input:not([type='checkbox']), select, textarea)) {
    width: 100%;
    max-width: 24rem;
    min-width: 0;
    box-sizing: border-box;
  }

  .field
    :global(:where(input:not([type='checkbox'], .ui-input), select, textarea:not(.ui-textarea))) {
    background: var(--surface-base);
    color: var(--text-primary);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2);
    font: inherit;
    font-size: var(--text-sm);
  }

  /* `font: inherit` above is a shorthand and carries the body's 1.5 leading
     with it, so an input here grew past the field tier it sits in the moment
     iOS floored its text at 16px. Pinned beside the shorthand rather than
     centrally for that reason. Textareas keep the inherited leading: they are
     sized by their rows and read as prose, and they stay in the sans face
     unless they are a TextArea with `monospace`. */
  .field :global(:where(input:not([type='checkbox'], .ui-input), select)) {
    line-height: var(--leading-tight);
  }

  .field :global(:where(textarea:not(.ui-textarea))) {
    resize: vertical;
  }

  .field-wide :global(:where(textarea, input)) {
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

  .field.checkbox :global(input[type='checkbox']) {
    width: auto;
  }

  /* The money `.ctl`: a dense field that grows from 9rem in a wrapping row. */
  .field.compact {
    gap: var(--space-1);
    flex: 1 1 9rem;
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
      margin: calc((var(--control-height-lg) - 1em) / 2) 0 0;
      width: 1em;
      height: 1em;
    }
  }

  .field-error {
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
  }
</style>
