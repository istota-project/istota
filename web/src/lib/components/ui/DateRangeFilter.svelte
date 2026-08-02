<script lang="ts">
  interface Props {
    /** Start of the range, `yyyy-mm-dd`. Bindable. */
    from: string;
    /** End of the range, `yyyy-mm-dd`. Bindable. */
    to: string;
    /** Runs on either input's `change`. */
    onChange?: () => void;
    /**
     * Render visible "From" / "To" captions. Off by default: a date input,
     * the word "to" and a second date input reads as a range on its own, and
     * dropping the captions buys most of a phone's width back. The inputs are
     * always named for a screen reader either way.
     */
    labelled?: boolean;
    /** The word between the two inputs when unlabelled. */
    separator?: string;
    /** Upper bound for both inputs — `max={today}` on a history filter. */
    max?: string;
    fromLabel?: string;
    toLabel?: string;
  }

  let {
    from = $bindable(''),
    to = $bindable(''),
    onChange,
    labelled = false,
    separator = 'to',
    max,
    fromLabel = 'From date',
    toLabel = 'To date',
  }: Props = $props();

  const uid = $props.id();
</script>

<!--
  A from/to date range filter. /health/history and /location/history each had
  one — the same flex row, the same input paint, the same webkit picker-icon
  filter — differing only in whether the captions were drawn and whether the
  inputs capped at today.

  Both had got the fiddly part right independently: the calendar picker icon
  ships dark, so the dark theme inverts it and the light theme has to undo
  that, and each page carried both halves of the pair — one of them a good
  two hundred lines away from the rest of its date CSS. That is the argument
  for the component rather than against it. The rule that is easy to miss is
  the rule that goes missing on the third page, and there is no third page
  yet only because nobody has added one.

  The inputs deliberately set no min-width. primitives.css floors it (an unset
  date input otherwise collapses to a blank sliver on iOS) and any rule here
  would outrank that floor — see dateInputs.test.ts, which is the net.
-->
<div class="date-range">
  {#if labelled}
    <label class="date-cap" for="{uid}-from">{fromLabel.replace(/ date$/i, '')}</label>
  {/if}
  <input
    id="{uid}-from"
    type="date"
    bind:value={from}
    onchange={onChange}
    {max}
    aria-label={fromLabel}
  />
  {#if labelled}
    <label class="date-cap" for="{uid}-to">{toLabel.replace(/ date$/i, '')}</label>
  {:else}
    <span class="date-sep">{separator}</span>
  {/if}
  <input id="{uid}-to" type="date" bind:value={to} onchange={onChange} {max} aria-label={toLabel} />
</div>

<style>
  .date-range {
    display: flex;
    align-items: center;
    /* A date input renders at a font-driven intrinsic width on iOS, and the
       touch floor in primitives.css lifts it to 16px — so two of them plus
       their captions no longer fit one phone row. Wrap rather than squeeze. */
    flex-wrap: wrap;
    min-width: 0;
    gap: var(--space-2);
  }

  .date-cap,
  .date-sep {
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .date-range input[type='date'] {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    color: var(--text-primary);
    font-family: inherit;
    font-size: var(--text-xs);
    /* design-lint-allow: hairline nudge below --space-1, matching the two
       filters this replaces. */
    padding: 0.2rem var(--space-2);
    border-radius: var(--radius-sm);
    /* No min-width: sizing is primitives.css's job. Restating it here would
       outrank the floor and collapse both inputs to blank slivers on iOS,
       which is a bug one of these filters already shipped. */
  }

  .date-range input[type='date']::-webkit-calendar-picker-indicator {
    /* design-lint-allow: not a color — a theme-conditional `filter` on a UA
       pseudo-element we cannot restyle directly. The icon ships dark, so the
       dark theme inverts it and the light theme must undo that. */
    filter: invert(0.7);
  }

  /* design-lint-allow: not a color — the other half of the `filter` pair
     above, undoing the dark theme's inversion on white. The rule has to name
     the theme because a UA pseudo-element cannot read a token through the
     shadow boundary reliably; a --calendar-icon-filter token pair in
     tokens.css would be the better home and is a follow-up. */
  :global(:root[data-theme='light'])
    .date-range
    input[type='date']::-webkit-calendar-picker-indicator {
    filter: none;
  }
</style>
