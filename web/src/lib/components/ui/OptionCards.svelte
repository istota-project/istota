<script lang="ts" module>
  export interface OptionCard {
    value: string;
    title: string;
    /** One line under the title. */
    description?: string;
    /** A short pill beside the title ("Recommended"). */
    badge?: string;
    disabled?: boolean;
  }
</script>

<script lang="ts">
  import type { Snippet } from 'svelte';
  import Badge from './Badge.svelte';

  interface Props {
    options: OptionCard[];
    /** The checked option's value, or null for none. */
    value?: string | null;
    /** The group's accessible name, since the cards carry no visible caption. */
    label: string;
    onchange?: (value: string) => void;
    /**
     * Drawn right after the checked card, for the fields that choice asks.
     * Outside the card itself, because the card is a `role="radio"` button
     * and an input inside a button is neither valid nor reachable.
     */
    selected?: Snippet<[string]>;
  }

  let { options, value = $bindable(null), label, onchange, selected }: Props = $props();

  const cards: HTMLButtonElement[] = $state([]);

  // The one card Tab lands on: the checked one, or the first that can be
  // checked. Every other card is reached with the arrows (WAI-ARIA radio group).
  const tabStop = $derived.by(() => {
    const checked = options.findIndex((o) => o.value === value && !o.disabled);
    return checked >= 0 ? checked : options.findIndex((o) => !o.disabled);
  });

  function choose(index: number) {
    const option = options[index];
    if (!option || option.disabled) return;
    if (option.value !== value) {
      value = option.value;
      onchange?.(option.value);
    }
  }

  function step(from: number, delta: number): number {
    for (let i = 1; i <= options.length; i++) {
      const next = (from + delta * i + options.length * i) % options.length;
      if (!options[next].disabled) return next;
    }
    return from;
  }

  function onkeydown(event: KeyboardEvent, index: number) {
    let next: number;
    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') next = step(index, 1);
    else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') next = step(index, -1);
    else if (event.key === 'Home') next = step(-1, 1);
    else if (event.key === 'End') next = step(options.length, -1);
    else return;
    event.preventDefault();
    choose(next);
    cards[next]?.focus();
  }
</script>

<!--
  A choice between two to four options, each with a line saying what it
  means — which `Select` would hide and `Chip`, a toggle, cannot carry. Arrow
  keys move the selection, as in a native radio group, and skip a disabled
  card.
-->
<div class="option-cards" role="radiogroup" aria-label={label}>
  {#each options as option, index (option.value)}
    {@const checked = option.value === value}
    <button
      bind:this={cards[index]}
      type="button"
      role="radio"
      class="option-card"
      class:checked
      aria-checked={checked}
      aria-disabled={option.disabled || undefined}
      tabindex={index === tabStop ? 0 : -1}
      onclick={() => choose(index)}
      onkeydown={(event) => onkeydown(event, index)}
    >
      <span class="dot" aria-hidden="true"></span>
      <span class="text">
        <span class="title"
          >{option.title}{#if option.badge}<Badge variant="info" size="sm">{option.badge}</Badge
            >{/if}</span
        >
        {#if option.description}<span class="description">{option.description}</span>{/if}
      </span>
    </button>
    {#if checked && selected}
      <div class="option-detail">{@render selected(option.value)}</div>
    {/if}
  {/each}
</div>

<style>
  .option-cards {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .option-card {
    display: flex;
    align-items: flex-start;
    gap: var(--space-3);
    width: 100%;
    padding: var(--space-3) var(--space-4);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    background: var(--surface-card);
    color: var(--text-primary);
    font: inherit;
    line-height: 1.4;
    text-align: left;
    cursor: pointer;
    transition: border-color var(--transition-fast);
  }

  .option-card:hover:not([aria-disabled='true']) {
    border-color: var(--border-hover);
  }

  /* An inset ring rather than a thicker border, so checking a card does not
     shift its text by the extra pixel. */
  .option-card.checked,
  .option-card.checked:hover {
    border-color: var(--accent-blue);
    box-shadow: inset 0 0 0 1px var(--accent-blue);
  }

  .option-card[aria-disabled='true'] {
    opacity: 0.5;
    cursor: not-allowed;
  }

  /* Sized in em against the title's line, so it tracks the text scale. */
  .dot {
    flex: none;
    width: 1em;
    height: 1em;
    margin-top: 0.2em;
    border: 1px solid var(--border-hover);
    border-radius: 50%;
    box-sizing: border-box;
  }

  .checked .dot {
    border: 0.3em solid var(--accent-blue);
  }

  .text {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    min-width: 0;
  }

  .title {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--text-sm);
    font-weight: 600;
  }

  .description {
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  .option-detail {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    padding: var(--space-1) 0 var(--space-2);
  }
</style>
