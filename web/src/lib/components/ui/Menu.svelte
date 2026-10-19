<script lang="ts" module>
  import type { Component, Snippet } from 'svelte';

  export interface MenuItem {
    label: string;
    icon?: Component;
    // Omit when the item is a link (`href`). One of the two must be set.
    onSelect?: () => void;
    // Renders the item as a real anchor so navigation keeps middle-click,
    // open-in-new-tab and the status-bar URL preview. A menu item that only
    // navigates should use this rather than an onSelect + goto().
    href?: string;
    target?: '_blank';
    download?: string;
    // Optional interception of a link; its normal menu selection still closes.
    onClick?: (event: MouseEvent) => void;
    // An href the server answers rather than a page (a `/chat/r/...` deep
    // link), so the client does a full navigation instead of routing it.
    reload?: boolean;
    // The page this link item points at is the current one.
    active?: boolean;
    danger?: boolean;
    disabled?: boolean;
    ariaLabel?: string;
    title?: string;
    // After the label, inside the item (the app nav's unread counts).
    trailing?: Snippet;
  }
</script>

<script lang="ts">
  import { DropdownMenu } from 'bits-ui';

  type Handler<E extends Event> = ((e: E) => void) | undefined;

  interface Props {
    items: MenuItem[];
    /** The button that opens the menu. Spread `props` onto it. */
    trigger: Snippet<[props: Record<string, unknown>]>;
    /** The menu's accessible name. */
    label: string;
    align?: 'start' | 'end';
    side?: 'top' | 'bottom';
    open?: boolean;
    /**
     * Leave focus where it is when a pointer opens the menu, and keep it there
     * through a tap on a row and the close: the chat composer, where moving
     * focus off the field drops the soft keyboard. Opened from the keyboard,
     * the menu still takes focus and arrow keys move through it.
     */
    retainFocus?: boolean;
  }

  let {
    items,
    trigger,
    label,
    align = 'end',
    side = 'bottom',
    open = $bindable(false),
    retainFocus = false,
  }: Props = $props();

  let triggerEl: HTMLElement | null = $state(null);
  let openedByPointer = false;

  const holding = () => retainFocus && openedByPointer;

  function holdFocus(e: Event) {
    if (holding()) e.preventDefault();
  }

  // Records how the menu was opened, ahead of bits-ui's own handlers. A click
  // with no detail is a screen reader's activation, which is keyboard-like.
  function triggerProps(props: Record<string, unknown>): Record<string, unknown> {
    if (!retainFocus) return props;
    const down = props.onpointerdown as Handler<PointerEvent>;
    const key = props.onkeydown as Handler<KeyboardEvent>;
    const click = props.onclick as Handler<MouseEvent>;
    return {
      ...props,
      onpointerdown: (e: PointerEvent) => {
        openedByPointer = true;
        down?.(e);
      },
      onkeydown: (e: KeyboardEvent) => {
        openedByPointer = false;
        key?.(e);
      },
      onclick: (e: MouseEvent) => {
        if (e.detail === 0) openedByPointer = false;
        click?.(e);
      },
    };
  }
</script>

{#snippet body(item: MenuItem)}
  {#if item.icon}<item.icon
      size={14}
      aria-hidden="true"
    />{/if}{item.label}{#if item.trailing}{@render item.trailing()}{/if}
{/snippet}

<DropdownMenu.Root bind:open>
  <DropdownMenu.Trigger bind:ref={triggerEl}>
    {#snippet child({ props })}
      {@render trigger(triggerProps(props))}
    {/snippet}
  </DropdownMenu.Trigger>
  <DropdownMenu.Portal>
    <DropdownMenu.Content
      class="ui-menu-content"
      aria-label={label}
      {side}
      {align}
      sideOffset={4}
      data-keeps-keyboard={retainFocus ? '' : undefined}
      onOpenAutoFocus={holdFocus}
      onCloseAutoFocus={holdFocus}
    >
      {#each items as item (item.label)}
        {@const cls = item.danger ? 'ui-menu-item ui-menu-item--danger' : 'ui-menu-item'}
        <DropdownMenu.Item
          class={cls}
          disabled={item.disabled}
          onSelect={item.onSelect}
          onmousedown={retainFocus ? holdFocus : undefined}
          onpointermove={retainFocus ? holdFocus : undefined}
        >
          {#snippet child({ props })}
            {#if item.href}
              <a
                {...props}
                href={item.href}
                target={item.target}
                rel={item.target === '_blank' ? 'noopener noreferrer' : undefined}
                download={item.download}
                data-sveltekit-reload={item.reload ? '' : undefined}
                aria-current={item.active ? 'page' : undefined}
                aria-label={item.ariaLabel}
                title={item.title}
                onclick={(event) => {
                  if (typeof props.onclick === 'function') props.onclick(event);
                  if (!item.disabled && item.onClick) {
                    // A viewer opened here must return to the surviving trigger.
                    triggerEl?.focus();
                    item.onClick(event);
                  }
                }}>{@render body(item)}</a
              >
            {:else}
              <div {...props} aria-label={item.ariaLabel} title={item.title}>
                {@render body(item)}
              </div>
            {/if}
          {/snippet}
        </DropdownMenu.Item>
      {/each}
    </DropdownMenu.Content>
  </DropdownMenu.Portal>
</DropdownMenu.Root>

<style>
  :global(.ui-menu-content) {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    padding: var(--space-1);
    z-index: var(--z-popover);
    box-shadow: var(--shadow-md);
    min-width: 9rem;
    /* Focused on open by bits-ui; the roving highlight is the affordance. */
    outline: none;
  }

  :global(.ui-menu-item) {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2);
    font-size: var(--text-sm);
    color: var(--text-secondary);
    text-decoration: none;
    border-radius: var(--radius-sm);
    cursor: pointer;
    outline: none;
    user-select: none;
    white-space: nowrap;
  }
  :global(.ui-menu-item[aria-current='page']) {
    color: var(--text-primary);
  }

  :global(.ui-menu-item[data-highlighted]) {
    background: var(--hover-bg);
    color: var(--hover-fg);
  }
  /* A retained-focus menu never moves focus onto a row under the mouse, so
     bits-ui sets no highlight there; hover stands in. Guarded because iOS
     synthesizes a hover on tap and leaves it on the last row touched. */
  @media (hover: hover) {
    :global(.ui-menu-content[data-keeps-keyboard] .ui-menu-item:hover) {
      background: var(--hover-bg);
      color: var(--hover-fg);
    }
  }
  :global(.ui-menu-item[data-disabled]) {
    opacity: 0.4;
    cursor: not-allowed;
  }
  :global(.ui-menu-item--danger[data-highlighted]) {
    color: var(--status-danger-fg);
  }
</style>
