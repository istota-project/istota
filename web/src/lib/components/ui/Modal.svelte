<script lang="ts" module>
  export type ViewerNavigation = {
    previousLabel: string;
    nextLabel: string;
    canPrevious: boolean;
    canNext: boolean;
    busy?: boolean;
    onPrevious: () => void;
    onNext: () => void | Promise<void>;
  };
</script>

<script lang="ts">
  import { untrack, type Snippet } from 'svelte';
  import { ChevronLeft, ChevronRight, LoaderCircle, X } from '@lucide/svelte';
  import IconButton from './IconButton.svelte';
  import { Dialog } from 'bits-ui';

  interface Props {
    variant?: 'default' | 'viewer';
    dismissible?: boolean;
    metadata?: Snippet;
    actions?: Snippet;
    navigation?: ViewerNavigation;
    bodyKey?: string | number;
    open: boolean;
    title: string;
    description?: string;
    onOpenChange?: (open: boolean) => void;
    children: Snippet;
    footer?: Snippet;
    width?: string;
    /**
     * Panel height. `auto` (the default) sizes to the content, capped by the
     * viewport. A dialog whose content is a list rather than a form wants the
     * height it can have — pass `100dvh` and the panel's own max-height caps
     * it to the safe box.
     */
    height?: string;
  }

  let {
    open = $bindable(false),
    title,
    description,
    onOpenChange,
    children,
    footer,
    width,
    variant = 'default',
    dismissible = true,
    metadata,
    actions,
    navigation,
    bodyKey,
    height = 'auto',
  }: Props = $props();

  let bodyEl: HTMLDivElement | undefined = $state();
  const viewer = $derived(variant === 'viewer');

  let previousBody: HTMLDivElement | undefined;
  let previousBodyKey: string | number | undefined;

  $effect(() => {
    // Only identity and opening the body trigger a reset, not header updates.
    const key = bodyKey;
    const body = bodyEl;
    if (body && untrack(() => viewer) && (body !== previousBody || key !== previousBodyKey)) {
      body.scrollTop = 0;
    }
    previousBody = body;
    previousBodyKey = key;
  });

  function handleKeydown(event: KeyboardEvent) {
    if (
      !viewer ||
      !navigation ||
      event.defaultPrevented ||
      event.isComposing ||
      event.altKey ||
      event.ctrlKey ||
      event.metaKey ||
      event.shiftKey
    )
      return;
    const target = event.target;
    if (
      !(target instanceof Element) ||
      target.closest('[role="dialog"]') !== event.currentTarget ||
      target.closest(
        'input, textarea, select, [contenteditable]:not([contenteditable="false"]), audio, video',
      )
    )
      return;
    if (event.key === 'ArrowLeft' && navigation.canPrevious) {
      event.preventDefault();
      navigation.onPrevious();
    } else if (event.key === 'ArrowRight' && navigation.canNext && !navigation.busy) {
      event.preventDefault();
      void navigation.onNext();
    }
  }
</script>

{#snippet previous()}
  {#if navigation}
    <IconButton
      label={navigation.previousLabel}
      disabled={!navigation.canPrevious}
      onclick={navigation.onPrevious}
    >
      <ChevronLeft size={20} />
    </IconButton>
  {/if}
{/snippet}

{#snippet next()}
  {#if navigation}
    <IconButton
      label={navigation.nextLabel}
      disabled={!navigation.canNext || navigation.busy}
      onclick={() => {
        void navigation?.onNext();
      }}
    >
      {#if navigation.busy}
        <span role="status" aria-label="Loading"><LoaderCircle size={20} /></span>
      {:else}<ChevronRight size={20} />{/if}
    </IconButton>
  {/if}
{/snippet}

{#snippet body()}
  <div class="ui-modal-body" bind:this={bodyEl}>{@render children()}</div>
  {#if footer}<div class="ui-modal-footer">{@render footer()}</div>{/if}
{/snippet}

<Dialog.Root bind:open {onOpenChange}>
  <Dialog.Portal>
    <Dialog.Overlay class={viewer ? 'ui-modal-overlay ui-viewer-overlay' : 'ui-modal-overlay'} />
    <Dialog.Content
      class={viewer
        ? `ui-modal-content ui-viewer-content${navigation ? ' ui-viewer-navigable' : ''}`
        : 'ui-modal-content'}
      style="--modal-width: {width ?? (viewer ? '720px' : '420px')}; --modal-height: {height}"
      onkeydown={handleKeydown}
      escapeKeydownBehavior={dismissible ? 'close' : 'ignore'}
      interactOutsideBehavior={dismissible ? 'close' : 'ignore'}
    >
      {#if viewer}
        {#if navigation}<div class="ui-viewer-previous">{@render previous()}</div>{/if}
        <div class="ui-viewer-panel">
          <header class="ui-viewer-header">
            <div class="ui-viewer-heading">
              <Dialog.Title class="ui-modal-title">{title}</Dialog.Title>
              {#if metadata}<div class="ui-viewer-metadata">{@render metadata()}</div>{/if}
            </div>
            <div class="ui-viewer-actions">
              {#if actions}{@render actions()}{/if}
              <IconButton
                label="Close"
                onclick={() => {
                  open = false;
                  onOpenChange?.(false);
                }}><X size={20} /></IconButton
              >
            </div>
            {#if navigation}
              <div class="ui-viewer-mobile-navigation">{@render previous()}{@render next()}</div>
            {/if}
          </header>
          {#if description}<Dialog.Description class="ui-modal-description"
              >{description}</Dialog.Description
            >{/if}
          {@render body()}
        </div>
        {#if navigation}<div class="ui-viewer-next">{@render next()}</div>{/if}
      {:else}
        <Dialog.Title class="ui-modal-title">{title}</Dialog.Title>
        {#if description}<Dialog.Description class="ui-modal-description"
            >{description}</Dialog.Description
          >{/if}
        {@render body()}
      {/if}
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>

<style>
  :global(.ui-modal-overlay) {
    position: fixed;
    inset: 0;
    background: var(--scrim-bg);
    z-index: var(--z-modal);
  }
  :global(.ui-modal-content) {
    position: fixed;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: var(--space-4);
    width: var(--modal-width, 420px);
    height: var(--modal-height, auto);
    /* Column so the body is the part that scrolls: the title (and the footer,
       which holds the actions) stay put instead of scrolling away from a long
       list. Inert at auto height, where the body never has to shrink. */
    display: flex;
    flex-direction: column;
    /* The panel is pinned to the viewport centre rather than laid out inside a
		   padded backdrop, so it can't use .overlay-safe — the insets come off its
		   caps instead. Subtracting both ends of each axis keeps a full-height modal
		   inside the safe box once it is centred, and dvh tracks a collapsing mobile
		   browser toolbar the way the body's height does. Inert where insets are 0. */
    max-width: calc(100vw - 2rem - var(--safe-left) - var(--safe-right));
    max-height: calc(100dvh - 2rem - var(--safe-top) - var(--safe-bottom));
    overflow: auto;
    z-index: var(--z-modal-panel);
    outline: none;
  }
  :global(.ui-modal-title) {
    font-size: var(--text-base);
    font-weight: 600;
    margin: 0 0 var(--space-2);
    color: var(--text-primary);
  }
  :global(.ui-modal-description) {
    font-size: var(--text-sm);
    color: var(--text-muted);
    margin: 0 0 var(--space-3);
  }
  /* min-height: 0 or the body refuses to shrink below its content and the
     panel overflows its own max-height instead of scrolling here. */
  :global(.ui-modal-body) {
    font-size: var(--text-sm);
    min-height: 0;
    overflow: auto;
    /* A scroll container clips at its padding box, and rings are drawn outside
       the box they belong to — a selection or focus ring on the first item of a
       flush-left row (the room colour picker) lost its left edge to that. The
       bleed gives the clip box a few px on each side; the negative margin takes
       them back out of the panel's own padding, so nothing moves. */
    padding-inline: 4px;
    margin-inline: -4px;
  }
  :global(.ui-modal-footer) {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-2);
    margin-top: var(--space-4);
    padding-top: var(--space-3);
    border-top: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }
  :global(.ui-viewer-overlay) {
    background: var(--scrim-viewer-bg);
    z-index: var(--z-viewer);
  }
  :global(.ui-viewer-content) {
    /* Center inside the safe box, including asymmetric landscape insets. */
    left: calc((100vw + var(--safe-left) - var(--safe-right)) / 2);
    top: calc((100dvh + var(--safe-top) - var(--safe-bottom)) / 2);
    padding: 0;
    border: 0;
    background: transparent;
    overflow: hidden;
    z-index: var(--z-viewer-control);
  }
  :global(.ui-viewer-navigable) {
    width: calc(var(--modal-width) + 7rem);
    display: grid;
    grid-template-columns: 3rem minmax(0, 1fr) 3rem;
    gap: var(--space-2);
  }
  .ui-viewer-panel {
    min-height: 0;
    min-width: 0;
    max-height: calc(100dvh - 2rem - var(--safe-top) - var(--safe-bottom));
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: var(--surface-card);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-card);
    box-shadow: var(--shadow-lg);
  }
  .ui-viewer-header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3);
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }
  .ui-viewer-heading {
    flex: 1;
    min-width: 0;
  }
  .ui-viewer-heading :global(.ui-modal-title) {
    font-size: var(--text-sm);
    margin: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .ui-viewer-metadata {
    color: var(--text-dim);
    font-size: var(--text-xs);
    overflow-wrap: anywhere;
  }
  .ui-viewer-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    max-width: 100%;
  }
  .ui-viewer-panel :global(.ui-modal-body) {
    padding: var(--space-2) var(--space-3);
    margin: 0;
  }
  .ui-viewer-panel :global(.ui-modal-description),
  .ui-viewer-panel :global(.ui-modal-footer) {
    margin: 0;
    padding: var(--space-2) var(--space-3);
  }
  .ui-viewer-previous,
  .ui-viewer-next {
    align-self: center;
    justify-self: center;
  }
  .ui-viewer-mobile-navigation {
    display: none;
  }
  @media (max-width: 640px) {
    :global(.ui-viewer-navigable) {
      display: flex;
      width: var(--modal-width);
      gap: 0;
    }
    .ui-viewer-previous,
    .ui-viewer-next {
      display: none;
    }
    .ui-viewer-mobile-navigation {
      display: flex;
      justify-content: flex-end;
      gap: var(--space-2);
      width: 100%;
    }
  }
</style>
