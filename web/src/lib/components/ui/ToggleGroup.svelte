<script lang="ts" module>
  import type { Component } from 'svelte';

  export interface ToggleOption {
    value: string;
    label: string;
    icon?: Component;
    disabled?: boolean;
  }
</script>

<script lang="ts">
  import Icon from './Icon.svelte';
  import { ToggleGroup } from 'bits-ui';
  import Chip from './Chip.svelte';
  import { getFieldContext } from './fieldContext';

  /**
   * A row of options drawn as chips. `single` is a segmented control: one
   * option is always on (pressing it again keeps it), and bits-ui makes the
   * options radios with arrow-key focus. `multiple` toggles each option on its
   * own, as `aria-pressed` buttons, for a set of flags (disabled modules).
   * Inside a `Field` the caption names the group; elsewhere `label` does.
   */
  interface Props {
    options: ToggleOption[];
    /** A string in single mode, a string array in multiple mode. Bindable. */
    value: string | string[];
    type?: 'single' | 'multiple';
    /** The group's accessible name, outside a Field. */
    label: string;
    /** Draw each option as its icon alone; the label becomes its aria-label and title. */
    iconOnly?: boolean;
    size?: 'sm' | 'md';
    disabled?: boolean;
    onValueChange?: (value: string | string[]) => void;
    class?: string;
  }

  let {
    options,
    value = $bindable(),
    type = 'single',
    label,
    iconOnly = false,
    size = 'sm',
    disabled = false,
    onValueChange,
    class: className = '',
  }: Props = $props();

  const field = getFieldContext();
  const owned = field?.claim('labelledby') ?? false;

  function setSingle(next: string) {
    // bits-ui clears a single group when its pressed item is pressed again; a
    // segmented control keeps its choice.
    if (!next || next === value) return;
    value = next;
    onValueChange?.(next);
  }

  function setMultiple(next: string[]) {
    value = next;
    onValueChange?.(next);
  }
</script>

{#snippet item(option: ToggleOption)}
  <ToggleGroup.Item value={option.value} disabled={option.disabled}>
    {#snippet child({ props, pressed })}
      <Chip
        {...props}
        checked={pressed}
        icon={iconOnly && !!option.icon}
        aria-label={iconOnly ? option.label : undefined}
        title={iconOnly ? option.label : undefined}
        >{#if option.icon}<Icon
            icon={option.icon}
            size="sm"
          />{/if}{#if !iconOnly}{option.label}{/if}</Chip
      >
    {/snippet}
  </ToggleGroup.Item>
{/snippet}

{#if type === 'single'}
  <ToggleGroup.Root
    type="single"
    bind:value={() => (typeof value === 'string' ? value : ''), setSingle}
    {disabled}
    class="ui-toggle-group {className}"
    data-size={size}
    aria-label={owned ? undefined : label}
    aria-labelledby={owned ? field?.labelId : undefined}
  >
    {#each options as option (option.value)}{@render item(option)}{/each}
  </ToggleGroup.Root>
{:else}
  <ToggleGroup.Root
    type="multiple"
    bind:value={() => (Array.isArray(value) ? value : []), setMultiple}
    {disabled}
    class="ui-toggle-group {className}"
    data-size={size}
    aria-label={owned ? undefined : label}
    aria-labelledby={owned ? field?.labelId : undefined}
  >
    {#each options as option (option.value)}{@render item(option)}{/each}
  </ToggleGroup.Root>
{/if}

<style>
  :global(.ui-toggle-group) {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1);
  }
  /* md lines the chips up with a default Button or a md Select beside them. */
  :global(.ui-toggle-group[data-size='md']) {
    --control-height-sm: var(--control-height-md);
  }
</style>
