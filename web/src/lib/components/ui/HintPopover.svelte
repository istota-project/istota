<script lang="ts">
  import Icon from './Icon.svelte';
  import { Popover } from 'bits-ui';
  import { TriangleAlert } from '@lucide/svelte';

  interface Props {
    /** The guidance text. Nothing renders when blank. */
    text?: string;
    /** Accessible name for the trigger; defaults to a generic phrasing. */
    label?: string;
    variant?: 'hint' | 'warning';
  }

  let { text, label = 'More information', variant = 'hint' }: Props = $props();

  /** The trigger props bits-ui hands to the `child` snippet, typed enough to
   *  re-invoke the click handler our own wrapper shadows. */
  type TriggerProps = Record<string, unknown> & {
    onclick?: (e: MouseEvent) => void;
  };
</script>

{#if text}
  <Popover.Root>
    <!--
      A Popover rather than a Tooltip so the hint is reachable by tap: bits-ui
      tooltips are hover/focus only by design, which would make every hint
      unreachable on a phone — and the settings pages are used there.
      `openOnHover` gives the desktop hover behaviour on top of the click and
      keyboard handling a popover already has.

      The trigger is rendered via `child` as a span, not the default button,
      because Field wraps its label text and its control in one
      <label>: a <button> is a labelable element, so a button trigger sitting
      before the input in tree order would become the label's control and
      clicking the field name would focus the "?" instead of the input.
    -->
    <Popover.Trigger openOnHover openDelay={200} closeDelay={150}>
      {#snippet child({ props })}
        {@const trigger = props as TriggerProps}
        <span
          {...trigger}
          role="button"
          tabindex="0"
          aria-label={label}
          class="ui-hint-trigger touch-target"
          class:warning={variant === 'warning'}
          onclick={(e) => {
            // This span sits inside the field's <label>, so a click on it also
            // activates the label and focuses the input behind the popover.
            // Our handler shadows the spread one, hence the explicit re-invoke.
            e.preventDefault();
            e.stopPropagation();
            trigger.onclick?.(e);
          }}
        >
          {#if variant === 'warning'}<Icon icon={TriangleAlert} size="sm" />{:else}?{/if}
        </span>
      {/snippet}
    </Popover.Trigger>
    <Popover.Portal>
      <Popover.Content class="ui-hint-content" sideOffset={6} align="start">
        {text}
      </Popover.Content>
    </Popover.Portal>
  </Popover.Root>
{/if}

<style>
  .ui-hint-trigger {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    /* em-relative so the badge tracks the text-size preference. */
    width: 1.15em;
    height: 1.15em;
    border-radius: var(--radius-round);
    border: 1px solid var(--border-default);
    color: var(--text-muted);
    font-size: var(--text-xs);
    line-height: var(--leading-none);
    cursor: help;
    user-select: none;
    flex: none;
  }

  .ui-hint-trigger:hover,
  .ui-hint-trigger:focus-visible {
    color: var(--text-primary);
    border-color: var(--text-muted);
  }

  .ui-hint-trigger.warning {
    width: 1.25rem;
    height: 1.25rem;
    border: none;
    border-radius: var(--radius-sm);
    color: var(--status-warn-fg);
  }

  :global(.ui-hint-content) {
    z-index: var(--z-popover);
    max-width: 22rem;
    background: var(--surface-card);
    color: var(--text-primary);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-2);
    font-size: var(--text-xs);
    line-height: var(--leading-snug);
    /* A config field's doc is paragraphs separated by blank lines. */
    white-space: pre-line;
    box-shadow: var(--shadow-md);
  }
</style>
