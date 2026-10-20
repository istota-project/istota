<script lang="ts">
  import Icon from './Icon.svelte';
  import type { Snippet } from 'svelte';
  import type { HTMLInputAttributes } from 'svelte/elements';
  import { Eye, EyeOff, X } from '@lucide/svelte';
  import IconButton from './IconButton.svelte';
  import { getFieldContext, joinIds } from './fieldContext';

  interface Props extends Omit<HTMLInputAttributes, 'value' | 'size' | 'prefix'> {
    value?: string | number | null;
    /** Marks the value as rejected — sets aria-invalid and the danger border. */
    invalid?: boolean;
    /** For paths, tokens and ids, where character shape carries meaning. */
    monospace?: boolean;
    /** `field` is a form's input; `toolbar` is a filter in a bar (`--surface-card`,
     *  the `--control-radius` corner, the compact control height). */
    size?: 'field' | 'toolbar';
    /** `ghost` draws no box until hovered or focused: an inline edit in a table. */
    variant?: 'default' | 'ghost';
    /** The table-cell editor: a tighter box for a grid of inputs. */
    dense?: boolean;
    /** Before the value, inside the box: "$", or a search glyph. A string is
     *  decoration, hidden from a screen reader, so the label must carry it. */
    prefix?: Snippet | string;
    /** After the value, inside the box: a unit, "of 12", a default marker. */
    suffix?: Snippet | string;
    /** An explicit Clear button while there is a value. Replaces the WebKit
     *  cancel glyph on a search input. */
    clearable?: boolean;
    /** A password input gets a show/hide button. No effect on another type. */
    revealable?: boolean;
    /** The input element, bindable, for a caller that focuses or measures it. */
    ref?: HTMLInputElement;
  }

  let {
    value = $bindable(''),
    type = 'text',
    invalid = false,
    monospace = false,
    size = 'field',
    variant = 'default',
    dense = false,
    prefix,
    suffix,
    clearable = false,
    revealable = false,
    ref = $bindable(),
    id,
    class: className = '',
    disabled,
    'aria-describedby': ariaDescribedBy,
    ...rest
  }: Props = $props();

  // Inside a Field the caption's `for` names this input.
  const field = getFieldContext();
  const owned = field?.claim('native', () => id) ?? false;

  let revealed = $state(false);
  const canReveal = $derived(revealable && type === 'password');
  const shownType = $derived(canReveal && revealed ? 'text' : type);
  const hasValue = $derived(value !== '' && value !== null && value !== undefined);
  const adorned = $derived(!!prefix || !!suffix || clearable || canReveal);
  const name = $derived((rest['aria-label'] as string | undefined) ?? 'password');

  function clear() {
    if (!ref) return;
    // Through the element, so `bind:value` and any `oninput` the caller passed
    // both see it, exactly as if the text had been deleted by hand.
    ref.value = '';
    ref.dispatchEvent(new Event('input', { bubbles: true }));
    ref.focus();
  }
</script>

<!--
  The text input. Unbound from any layout, because the label + hint + warning
  arrangement is Field's job; use them together for a form row, and this alone
  in a toolbar, a table cell, or anywhere a bare control is the whole control.

  With nothing to put beside the value it is a bare <input>, so a page can
  place it as a flex child by its own element. A prefix, suffix, Clear or
  reveal button wraps it in a box that carries the look, and the input inside
  draws nothing of its own.
-->
{#snippet adornment(part: Snippet | string, cls: string)}
  {#if typeof part === 'string'}
    <span class="ui-input-adorn {cls}" aria-hidden="true">{part}</span>
  {:else}
    <span class="ui-input-adorn {cls}">{@render part()}</span>
  {/if}
{/snippet}

{#snippet control(cls: string)}
  <input
    {...rest}
    type={shownType}
    bind:value
    bind:this={ref}
    {disabled}
    id={id ?? (owned ? field?.controlId : undefined)}
    class={cls}
    class:mono={monospace}
    aria-invalid={invalid || (owned && field?.invalid) || undefined}
    aria-describedby={joinIds(ariaDescribedBy, owned ? field?.describedBy : undefined)}
    aria-required={(owned && field?.required) || undefined}
  />
{/snippet}

{#if adorned}
  <span
    class="ui-input-box size-{size} {className}"
    class:ghost={variant === 'ghost'}
    class:dense
    class:invalid
    class:disabled={!!disabled}
    class:clearable
  >
    {#if prefix}{@render adornment(prefix, 'ui-input-prefix')}{/if}
    {@render control('ui-input-inner')}
    {#if suffix}{@render adornment(suffix, 'ui-input-suffix')}{/if}
    {#if clearable && hasValue && !disabled}
      <IconButton label="Clear" size="sm" class="ui-input-action" onclick={clear} icon={X} />
    {/if}
    {#if canReveal}
      <IconButton
        label="Show {name}"
        size="sm"
        class="ui-input-action"
        pressed={revealed}
        disabled={!!disabled}
        onclick={() => (revealed = !revealed)}
      >
        {#if revealed}<Icon icon={EyeOff} size="sm" />{:else}<Icon icon={Eye} size="sm" />{/if}
      </IconButton>
    {/if}
  </span>
{:else}
  {@render control(
    `ui-input size-${size}${variant === 'ghost' ? ' ghost' : ''}${dense ? ' dense' : ''}${invalid ? ' invalid' : ''} ${className}`,
  )}
{/if}

<style>
  /* The box: the bare input, or the wrapper an adorned one sits in. */
  .ui-input,
  .ui-input-box {
    width: 100%;
    box-sizing: border-box;
    background: var(--surface-base);
    color: var(--text-primary);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    font-size: var(--text-sm);
    /* Inert on its own — the text and padding already exceed this — but it is
       how the input joins a `.control-row`, where the container raises both
       compact tiers to the field height and every control in the row settles
       on the same number. Not on the --control-radius channel: a form input is
       always the field shape, in a row or out of one. */
    min-height: var(--control-height-md);
  }

  /* Not on a date or time input: primitives.css floors those at a date's
     width, and this would outrank the floor (dateInputs.test.ts). */
  .ui-input:where(
    :not([type='date'], [type='datetime-local'], [type='time'], [type='month'], [type='week'])
  ),
  .ui-input-box {
    min-width: 0;
  }

  .ui-input,
  .ui-input-inner {
    /* Vertical padding is half the horizontal: the box is set by min-height
       and the value centres in it, so a taller pad only inflates the control. */
    padding: var(--space-1) var(--space-2);
    font: inherit;
    font-size: var(--text-sm);
    /* `font: inherit` is a shorthand, so it also pulls in the body's 1.5
       leading, which made the box taller than the tier it sits in once iOS
       floored the text at 16px. Pinned beside the shorthand that would win. */
    line-height: var(--leading-tight);
  }

  .ui-input[type='search'],
  .ui-input-inner[type='search'] {
    -webkit-appearance: none;
    appearance: none;
  }

  .ui-input-box {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding: 0 var(--space-1) 0 var(--space-2);
  }

  /* The input inside a box draws nothing: the box is the control's edge. */
  .ui-input-inner {
    flex: 1 1 auto;
    width: 100%;
    min-width: 0;
    align-self: stretch;
    background: transparent;
    color: inherit;
    border: none;
    padding-inline: 0;
  }

  /* A number in a box carries its unit beside it, and the spin buttons would
     sit between the value and that unit. The money taxes inputs and the
     config editor's number field both hid them before they were Inputs. */
  .ui-input-inner[type='number'] {
    -moz-appearance: textfield;
    appearance: textfield;
  }

  .ui-input-inner[type='number']::-webkit-outer-spin-button,
  .ui-input-inner[type='number']::-webkit-inner-spin-button {
    -webkit-appearance: none;
    margin: 0;
  }

  .ui-input-adorn {
    flex: none;
    display: inline-flex;
    align-items: center;
    color: var(--text-muted);
    white-space: nowrap;
  }

  .ui-input-box :global(.ui-input-action) {
    flex: none;
  }

  /* A clearable search draws its own Clear, so the WebKit glyph would be a
     second one beside it. */
  .ui-input-box.clearable .ui-input-inner::-webkit-search-cancel-button {
    -webkit-appearance: none;
    display: none;
  }

  /* The box's focus state is the text field's: its border in the field token
     (primitives.css draws it for the bare input). */
  .ui-input-box:focus-within:not(.invalid) {
    border-color: var(--focus-field-border);
  }

  .ui-input.mono,
  .ui-input-inner.mono {
    font-family: var(--font-mono);
  }

  .ui-input.invalid,
  .ui-input-box.invalid {
    border-color: var(--status-danger-fg);
  }

  .ui-input:disabled,
  .ui-input-box.disabled {
    opacity: var(--disabled-opacity);
    cursor: not-allowed;
  }

  /* A filter in a bar: the paint `DateRangeFilter` and the money toolbars
     shared, at the compact control height a `.control-row` lifts. */
  .size-toolbar {
    background: var(--surface-card);
    border-radius: var(--control-radius);
    min-height: var(--control-height-sm);
  }

  /* No box until it is pointed at or focused: a table cell that edits in
     place, so a column of values reads as text until you reach for one. The
     transparent border keeps the box from shifting when it appears. */
  .ghost {
    background: transparent;
    border-color: transparent;
    padding-inline: var(--space-1);
  }

  .ghost:hover {
    background: var(--surface-base);
  }

  /* The ghost's own transparency outranks the shared focus rule, so its focus
     state is restated here in the same token. */
  .ui-input.ghost:focus-visible,
  .ui-input-box.ghost:focus-within {
    border-color: var(--focus-field-border);
    background: var(--surface-base);
  }

  /* The grid of inputs a lab panel or an import review is edited in: the
     compact height and a step down in type, so the editors do not overpower
     the table around them. */
  .ui-input.dense,
  .ui-input-box.dense {
    min-height: var(--control-height-sm);
    font-size: var(--text-xs);
  }

  .ui-input.dense,
  .dense .ui-input-inner {
    padding-block: 0;
    font-size: var(--text-xs);
  }

  .ui-input.dense {
    padding-inline: var(--space-1);
  }

  /* The type floor in primitives.css is declared on the control itself, so a
     string adornment beside it would stay at the desktop size while the value
     rises to 16px. Same floor, on the box. */
  @media (pointer: coarse) {
    .ui-input-box {
      --text-xs: max(0.7rem, 16px);
      --text-sm: max(0.75rem, 16px);
    }
  }
</style>
