<script lang="ts">
  import { Select as BitsSelect } from 'bits-ui';
  import { ChevronDown } from 'lucide-svelte';

  export interface SelectOption {
    value: string;
    label: string;
  }

  interface Props {
    value: string;
    options: SelectOption[];
    onValueChange?: (value: string) => void;
    placeholder?: string;
    disabled?: boolean;
    ariaLabel?: string;
    /** Render as a full-width control matching text inputs (settings forms). */
    fullWidth?: boolean;
    /**
     * Which trigger edge the popover lines up with.
     *
     * `start` by default: the list is at least as wide as the trigger
     * (`--bits-select-anchor-width`) and often wider, and a centred popover
     * then overhangs both sides of a compact trigger instead of reading as
     * belonging to it.
     */
    align?: 'start' | 'center' | 'end';
  }

  let {
    value = $bindable(''),
    options,
    onValueChange,
    placeholder = 'Select…',
    disabled = false,
    ariaLabel,
    fullWidth = false,
    align = 'start',
  }: Props = $props();

  const selectedLabel = $derived(options.find((o) => o.value === value)?.label ?? placeholder);
</script>

<BitsSelect.Root type="single" bind:value {onValueChange} {disabled}>
  <BitsSelect.Trigger
    class={fullWidth ? 'ui-select-trigger ui-select-trigger--full' : 'ui-select-trigger'}
    aria-label={ariaLabel}
  >
    <span class="ui-select-label">{selectedLabel}</span>
    <ChevronDown size={12} />
  </BitsSelect.Trigger>
  <BitsSelect.Portal>
    <!-- collisionPadding matches the tightest page gutter in the app (the
         health frame's 0.75rem mobile padding), so on a phone the popover
         stops at the same inset as the content it belongs to rather than
         running to the screen edge. -->
    <BitsSelect.Content class="ui-select-content" sideOffset={4} {align} collisionPadding={12}>
      <BitsSelect.Viewport class="ui-select-viewport">
        {#each options as opt (opt.value)}
          <BitsSelect.Item value={opt.value} label={opt.label} class="ui-select-item">
            {opt.label}
          </BitsSelect.Item>
        {/each}
      </BitsSelect.Viewport>
    </BitsSelect.Content>
  </BitsSelect.Portal>
</BitsSelect.Root>

<style>
  :global(.ui-select-trigger) {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    background: var(--surface-card);
    color: var(--text-primary);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-pill);
    padding: 0.15rem 0.5rem;
    font: inherit;
    font-size: var(--text-xs);
    line-height: 1.2;
    cursor: pointer;
    transition: background var(--transition-fast);
    /* The label below caps at 220px, which on a narrow phone is most of the
       screen — keep the trigger inside its container so a long selection
       cannot push a flex row past the page gutter. */
    max-width: 100%;
    min-width: 0;
    box-sizing: border-box;
  }
  :global(.ui-select-trigger:hover) {
    background: var(--surface-raised);
  }

  /* Full-width variant: matches the settings text inputs exactly — width:100%
	   capped at the same 24rem (border-box). On screens narrower than 24rem the
	   width:100% already shrinks it to fill the container, same as the inputs,
	   so no mobile-specific override is needed. */
  :global(.ui-select-trigger--full) {
    display: flex;
    justify-content: space-between;
    width: 100%;
    max-width: 24rem;
    box-sizing: border-box;
    background: var(--surface-base);
    border-radius: 0.3rem;
    padding: 0.3rem 0.5rem;
    font-size: var(--text-sm);
    /* Match native text-input height: inputs inherit line-height 1.5, but the
		   base trigger pins 1.2, which left the full-width trigger ~4px shorter
		   than the inputs it sits beside in forms. */
    line-height: 1.5;
  }
  :global(.ui-select-trigger--full .ui-select-label) {
    max-width: none;
  }
  :global(.ui-select-trigger:disabled) {
    opacity: 0.5;
    cursor: not-allowed;
  }
  :global(.ui-select-label) {
    max-width: 220px;
    /* Flex children floor at their content width, so without this the label
       refuses to shrink and the 220px cap wins over the trigger's max-width
       on a narrow screen. */
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  :global(.ui-select-content) {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: 0.4rem;
    padding: 0.25rem;
    z-index: 100;
    box-shadow: var(--shadow-md);
    /* design-lint-allow: bits-ui sets --bits-select-* on the popover at open
       time, so it is defined at runtime rather than in the token roster. */
    min-width: var(--bits-select-anchor-width, 8rem);
    /* Never wider than the space left after collisionPadding. Option labels
       are content (a condition name, a provider), so without this a long one
       sets the popover width and pushes it off a phone screen. */
    /* design-lint-allow: bits-ui runtime property, as above. */
    max-width: var(--bits-select-content-available-width, 100vw);
    box-sizing: border-box;
    max-height: 18rem;
    overflow: auto;
    outline: none;
  }

  :global(.ui-select-viewport) {
    display: flex;
    flex-direction: column;
    gap: 0.05rem;
  }

  :global(.ui-select-item) {
    padding: 0.3rem 0.5rem;
    font-size: var(--text-sm);
    color: var(--text-secondary);
    border-radius: 0.3rem;
    cursor: pointer;
    outline: none;
    user-select: none;
    /* Wrap rather than widen: once the popover is capped above, a long label
       has to fold onto a second line or it would be clipped. */
    white-space: normal;
    overflow-wrap: anywhere;
  }
  :global(.ui-select-item[data-highlighted]) {
    background: var(--surface-raised);
    color: var(--text-primary);
  }
  :global(.ui-select-item[data-selected]) {
    color: var(--text-primary);
  }
</style>
