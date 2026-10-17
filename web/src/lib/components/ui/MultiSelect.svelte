<script lang="ts">
  import { Combobox } from 'bits-ui';
  import { Check, X } from '@lucide/svelte';
  import Input from './Input.svelte';
  import EmptyState from './EmptyState.svelte';
  import type { SelectOption } from './Select.svelte';

  interface Props {
    value: string[];
    options: SelectOption[];
    onValueChange?: (value: string[]) => void;
    placeholder?: string;
    /** Shown in place of the chips when nothing is picked. */
    emptyLabel?: string;
    disabled?: boolean;
    ariaLabel?: string;
  }

  let {
    value = $bindable([]),
    options,
    onValueChange,
    placeholder = 'Search…',
    emptyLabel = 'None selected',
    disabled = false,
    ariaLabel,
  }: Props = $props();

  let open = $state(false);
  let search = $state('');
  let anchor = $state<HTMLElement | null>(null);

  const labels = $derived(new Map(options.map((o) => [o.value, o.label])));
  /** A disabled option that is picked stays picked: no remove button. */
  const locked = $derived(new Set(options.filter((o) => o.disabled).map((o) => o.value)));
  const filtered = $derived.by(() => {
    const query = search.trim().toLowerCase();
    if (!query) return options;
    return options.filter((o) => o.label.toLowerCase().includes(query));
  });

  function change(next: string[]) {
    value = next;
    onValueChange?.(next);
    // bits-ui writes the picked label into the input; this field is a search, so empty it.
    search = '';
    queueMicrotask(() => {
      const input = anchor?.querySelector('input');
      if (input) input.value = '';
    });
  }

  function remove(item: string) {
    change(value.filter((v) => v !== item));
  }
</script>

<!--
  A list of picks out of a long option set: the picks as removable chips, and
  a search field that opens bits-ui's multiple Combobox. The popover and its
  items wear Select's `ui-select-*` classes, so the two lists look the same.
-->
<div class="multiselect">
  <div class="picked" aria-live="polite">
    {#each value as item (item)}
      <span class="pick">
        <span class="pick-label">{labels.get(item) ?? item}</span>
        {#if !disabled && !locked.has(item)}<button
            type="button"
            class="pick-remove"
            aria-label="Remove {labels.get(item) ?? item}"
            onclick={() => remove(item)}><X size={12} /></button
          >{/if}
      </span>
    {:else}
      <span class="caption">{emptyLabel}</span>
    {/each}
  </div>
  {#if !disabled}
    <Combobox.Root
      type="multiple"
      {value}
      onValueChange={change}
      bind:open
      onOpenChange={(next) => {
        if (!next) search = '';
      }}
    >
      <div class="field" bind:this={anchor}>
        <Combobox.Input
          oninput={(e) => (search = e.currentTarget.value)}
          onclick={() => (open = true)}
          aria-label={ariaLabel}
        >
          {#snippet child({ props })}<Input {...props} {placeholder} />{/snippet}
        </Combobox.Input>
      </div>
      <Combobox.Portal>
        <!-- No Combobox.Trigger here, so the list is anchored to the field itself. -->
        <Combobox.Content
          class="ui-select-content ui-multiselect-content"
          customAnchor={anchor}
          align="start"
          sideOffset={4}
          collisionPadding={12}
        >
          <Combobox.Viewport class="ui-select-viewport">
            {#each filtered as opt (opt.value)}
              <Combobox.Item
                value={opt.value}
                label={opt.label}
                disabled={opt.disabled}
                class="ui-select-item ui-multiselect-item"
              >
                {#snippet children({ selected })}
                  <span class="ui-multiselect-check" aria-hidden="true"
                    >{#if selected}<Check size={14} />{/if}</span
                  >
                  {opt.label}
                {/snippet}
              </Combobox.Item>
            {:else}
              <EmptyState size="sm">No matches</EmptyState>
            {/each}
          </Combobox.Viewport>
        </Combobox.Content>
      </Combobox.Portal>
    </Combobox.Root>
  {/if}
</div>

<style>
  .multiselect {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    width: 100%;
    max-width: 24rem;
  }
  .picked {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
  }
  .pick {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    min-height: var(--control-height-sm);
    padding: 0 var(--space-2);
    border-radius: var(--control-radius);
    background: var(--surface-raised);
    box-shadow: inset 0 0 0 1px var(--border-hover);
    color: var(--text-primary);
    font-size: var(--text-sm);
  }
  .pick:has(.pick-remove) {
    padding-right: 0;
  }
  .pick-remove {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: var(--space-1);
    border: none;
    border-radius: var(--control-radius);
    background: none;
    color: var(--text-muted);
    cursor: pointer;
  }
  .pick-remove:hover {
    color: var(--text-primary);
  }
  /* Select's content rule sizes from the --bits-select-* pair; a Combobox
     publishes the same values under its own name. */
  :global(.ui-multiselect-content) {
    /* design-lint-allow: bits-ui runtime property, set on the popover at open. */
    min-width: var(--bits-combobox-anchor-width, 8rem);
    /* design-lint-allow: bits-ui runtime property, as above. */
    max-width: var(--bits-combobox-content-available-width, 100vw);
  }
  :global(.ui-multiselect-item) {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
  :global(.ui-multiselect-check) {
    display: inline-flex;
    width: 14px;
    flex-shrink: 0;
  }
</style>
