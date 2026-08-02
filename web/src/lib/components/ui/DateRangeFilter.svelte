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
    /** In-field text drawn while an input has no value. */
    fromPlaceholder?: string;
    toPlaceholder?: string;
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
    fromPlaceholder = 'Start',
    toPlaceholder = 'End',
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

  That floor gives an empty input a box; it cannot give it any content. iOS
  renders an unset date as *nothing* — no `mm/dd/yyyy` hint the way desktop
  has one — so the filter read as two blank rounded rectangles with nothing
  saying they were dates. The placeholder below is drawn over the field, and
  the native hint is suppressed while it shows, so both platforms render the
  same thing rather than one carrying a hint the other lacks.
-->
<div class="date-range">
  {#if labelled}
    <label class="date-cap" for="{uid}-from">{fromLabel.replace(/ date$/i, '')}</label>
  {/if}
  <span class="date-field" class:date-unset={!from}>
    <input
      id="{uid}-from"
      type="date"
      bind:value={from}
      onchange={onChange}
      {max}
      aria-label={fromLabel}
    />
    {#if !from}<span class="date-ph" aria-hidden="true">{fromPlaceholder}</span>{/if}
  </span>
  {#if labelled}
    <label class="date-cap" for="{uid}-to">{toLabel.replace(/ date$/i, '')}</label>
  {:else}
    <span class="date-sep">{separator}</span>
  {/if}
  <span class="date-field" class:date-unset={!to}>
    <input
      id="{uid}-to"
      type="date"
      bind:value={to}
      onchange={onChange}
      {max}
      aria-label={toLabel}
    />
    {#if !to}<span class="date-ph" aria-hidden="true">{toPlaceholder}</span>{/if}
  </span>
</div>

<style>
  .date-range {
    display: flex;
    align-items: center;
    /* A range is one control. Wrapping inside it stacked input / "to" / input
       into a three-line column beside the filter's Select on a phone, which
       reads as three separate fields rather than one range. The pair holds
       one line and gives ground by shrinking instead. */
    flex-wrap: nowrap;
    min-width: 0;
    /* No gap: the only place the row wants air is either side of the word
       between the fields, and that word can carry it. A gap here spends the
       same width three times over on the phone row this has to fit. */
  }

  /* Only there to be the placeholder's containing block, so it is as close to
     the input's own box as a wrapper can be: a flex box wrapping one
     stretched item has no line box, and therefore none of the baseline
     leading an inline wrapper would add under the field.

     Carries no state class of its own beyond `date-unset`. `empty` was the
     obvious name and the wrong one: primitives.css publishes `.empty` as a
     shared empty-state block, and a bare global class name lands on any
     element that happens to use it — the field silently took its `2rem 1rem`,
     which was the whole of the vertical gap this filter grew. */
  .date-field {
    position: relative;
    display: flex;
    /* The floor in primitives.css is `min(100%, 8em)` — the input can only
       stay inside a narrow row if the box it measures that 100% against can
       itself shrink. Set on the wrapper, not the input, so it does not
       outrank that floor (see dateInputs.test.ts). */
    flex: 1 1 auto;
    min-width: 0;
    /* A date is ~10 characters wide and never more. Past that the field is a
       wider box around the same value, so cap it rather than let a desktop
       row stretch two of them across the bar. */
    max-width: 9rem;
  }

  .date-cap,
  .date-sep {
    flex: 0 0 auto;
    /* The row's spacing, in the one place it is wanted (see .date-range). */
    padding: 0 var(--space-1);
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  /* Paint only. Height, corner and leading come from the field tier the
     container sets — the same division `.money-control-input` draws, and what
     keeps this level with the Select beside it once iOS floors its text. */
  .date-range input[type='date'] {
    width: 100%;
    box-sizing: border-box;
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--control-radius);
    color: var(--text-primary);
    font-family: inherit;
    font-size: var(--text-xs);
    line-height: 1.2;
    /* design-lint-allow: hairline nudge below --space-1, matching the two
       filters this replaces. */
    padding: 0.2rem var(--space-2);
    /* No min-width: sizing is primitives.css's job. Restating it here would
       outrank the floor and collapse both inputs to blank slivers on iOS,
       which is a bug one of these filters already shipped. */
  }

  /* Drawn over the field, so it sits where the value will and adds nothing to
     the box. `aria-hidden` and no pointer target: the input carries the
     label, and a tap anywhere in the field has to reach the picker.

     Hidden until the @supports below turns it on. Desktop WebKit/Blink do
     draw an empty date as `mm/dd/yyyy`, which would sit under this — so the
     pair is gated on the browser whose native hint we can actually suppress.
     Elsewhere (Firefox) nothing is drawn rather than two hints overlapping. */
  .date-ph {
    position: absolute;
    inset: 0;
    display: none;
    align-items: center;
    padding: 0 calc(var(--space-2) + 1px);
    font-size: var(--text-xs);
    line-height: 1.2;
    color: var(--text-dim);
    pointer-events: none;
  }

  /* The type floor in primitives.css is declared on the control itself, so a
     decorative sibling of one is outside it by design — and a 12px hint above
     a 16px value is the mismatch you see the moment a date is picked. Same
     mechanism, same `max()`, one token. */
  @media (pointer: coarse) {
    .date-ph {
      --text-xs: max(0.7rem, 16px);
    }
  }

  @supports selector(::-webkit-datetime-edit) {
    .date-ph {
      display: flex;
    }
    /* On focus the native segments are what you are typing into, so hand them
       back and drop ours. */
    .date-field input[type='date']:focus + .date-ph {
      display: none;
    }
    .date-field.date-unset input[type='date']:not(:focus)::-webkit-datetime-edit {
      opacity: 0;
    }
  }

  /* One rule for both themes. The glyph is drawn by a UA pseudo-element we
     cannot restyle, only filter, and it ships dark — the token carries which
     way each theme needs it, so there is no theme-conditional rule here and
     no half of a pair to forget. */
  .date-range input[type='date']::-webkit-calendar-picker-indicator {
    filter: var(--calendar-icon-filter);
  }
</style>
