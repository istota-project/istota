<script lang="ts">
  /**
   * The notification inbox, hung off whatever control the caller passes as its
   * `trigger`.
   *
   * A bits-ui `Popover` with a `Portal`, `align="end"` and `sideOffset={4}`,
   * following `HintPopover` for the trigger-as-`child` shape and `KebabMenu` for
   * the anchoring. Below 768px it stops being an anchored popover and becomes a
   * full-width fixed sheet — at panel width a phone would get a column narrower
   * than the titles it holds.
   *
   * **It defaults to "All", and that is load-bearing rather than a default.** A
   * fire-and-forget row — a task failed, an alert the model raised — has no
   * object whose state change would ever close it, so what closes it is being
   * seen. If the landing tab were "Needs action" those rows would never render,
   * never be seen, never resolve, and the badge would climb forever for anyone
   * who lived in that filter. "Needs action" is a filter the user selects.
   *
   * **Tab counts come from the list response, never from the badge.**
   * `total_open` is the post-sweep total and the rows are what rendered, so a
   * label reading "Needs action (3)" cannot sit above a visibly shorter list.
   * The badge is allowed to be briefly stale precisely because nothing is next
   * to it to contradict it; a label is.
   *
   * No `{@html}` here or in the three components below it.
   */
  import type { Snippet } from 'svelte';
  import { Popover } from 'bits-ui';
  import type { NotificationAction, ResolvedNotification } from '$lib/api';
  import {
    actionableCount,
    dismissNotification,
    markPanelSeen,
    notificationItems,
    notificationTotalOpen,
    notificationsError,
    notificationsLoading,
    refreshItems,
    runAction,
    seenPairs,
    type NotificationFilter,
  } from '$lib/stores/notifications';
  import Chip from './Chip.svelte';
  import NotificationDetail from './NotificationDetail.svelte';
  import NotificationItem from './NotificationItem.svelte';

  interface Props {
    /** The control the panel hangs off. Receives the props bits-ui needs
     *  spread onto a real element — see `NotificationBell`. */
    trigger: Snippet<[Record<string, unknown>]>;
  }

  let { trigger }: Props = $props();

  let open = $state(false);
  let filter = $state<NotificationFilter>('all');
  let detailItem = $state<ResolvedNotification | null>(null);

  async function load(next: NotificationFilter) {
    filter = next;
    // Seen is reported from the rows this call *returned*, never from the store.
    // `refreshItems` deliberately leaves the previous list on screen when it
    // fails, and returns null then and when a newer request has superseded it —
    // so reading the store here would stamp `seen_at` on rows the user is not
    // being shown, behind an error banner, and on a failed open would report a
    // list nothing had just fetched.
    const items = await refreshItems(next);
    if (!items) return;
    // `(id, updated_at)` pairs, not bare ids: the version is what stops a row
    // bumped between the fetch and this call being closed by a user who never
    // saw the new occurrence.
    void markPanelSeen(seenPairs(items));
  }

  function onOpenChange(next: boolean) {
    open = next;
    // **Every** open lands on "All", not just the first. The component stays
    // mounted for the life of the tab, so a filter that persisted would make
    // "Needs action" sticky — and the spec's reason for the default is that
    // opening the bell renders both classes, so a fire-and-forget row is seen
    // on any ordinary use of the feature. Sticky, a user who picked "Needs
    // action" once would never render that class again, so it would never be
    // seen, never resolve, and climb until the 14-day sweep caught it: exactly
    // the unbounded case the "All" default exists to close.
    if (next) void load('all');
  }

  function openDetail(item: ResolvedNotification) {
    detailItem = item;
    // Close the panel behind it. Two reasons, and the second is the hard one:
    // a list and a detail of one of its rows are the same thing twice, and on
    // a phone the panel is a full-width fixed sheet that the modal would have
    // to sit on top of -- but `--z-popover` (100) is deliberately *above*
    // `--z-modal` (50), because a Select opened inside a dialog has to clear
    // the dialog. So the modal renders under the panel, and no z-index on the
    // modal can fix it without breaking that rule for every other dialog.
    // Closing the panel removes the overlap instead of re-ordering it.
    open = false;
  }

  function onAction(item: ResolvedNotification, action: NotificationAction) {
    detailItem = null;
    void runAction(item.id, action);
  }

  function onDismiss(item: ResolvedNotification) {
    detailItem = null;
    void dismissNotification(item.id);
  }
</script>

<Popover.Root bind:open {onOpenChange}>
  <Popover.Trigger>
    {#snippet child({ props })}
      {@render trigger(props)}
    {/snippet}
  </Popover.Trigger>
  <Popover.Portal>
    <Popover.Content class="ui-notification-panel" align="end" sideOffset={4}>
      <div class="panel-head">
        <span class="micro-label">Notifications</span>
        <div class="panel-filters">
          <Chip
            checked={filter === 'all'}
            onclick={() => load('all')}
            aria-pressed={filter === 'all'}>All ({$notificationTotalOpen})</Chip
          >
          <Chip
            checked={filter === 'action'}
            onclick={() => load('action')}
            aria-pressed={filter === 'action'}
            >Needs action ({actionableCount($notificationItems)})</Chip
          >
        </div>
      </div>

      {#if $notificationsError}
        <p class="banner danger panel-msg">{$notificationsError}</p>
      {/if}

      <div class="panel-list">
        {#if $notificationItems.length}
          {#each $notificationItems as item (item.id)}
            <NotificationItem {item} onOpen={openDetail} {onAction} />
          {/each}
        {:else if $notificationsLoading}
          <p class="empty small">Loading…</p>
        {:else if filter === 'action'}
          <p class="empty small">Nothing needs your attention.</p>
        {:else}
          <p class="empty small">Nothing waiting on you.</p>
        {/if}
      </div>

      <!-- Not gated on the filter. `total_open` is the whole open set either
           way, so on "Needs action" this reads "Showing 4 of 60 open" — which is
           what that tab means. Gated on `all`, the one tab that can hide rows
           without saying so was the filtered one. -->
      {#if $notificationTotalOpen > $notificationItems.length}
        <p class="panel-foot caption">
          Showing {$notificationItems.length} of {$notificationTotalOpen} open.
        </p>
      {/if}
    </Popover.Content>
  </Popover.Portal>
</Popover.Root>

<NotificationDetail
  item={detailItem}
  open={detailItem !== null}
  onOpenChange={(next) => {
    if (!next) detailItem = null;
  }}
  {onAction}
  {onDismiss}
/>

<style>
  /* Portalled to <body>, so the class arrives as a prop string Svelte cannot
     hash — global, like HintPopover's own content class. */
  :global(.ui-notification-panel) {
    z-index: var(--z-popover);
    width: 24rem;
    max-width: calc(100vw - var(--space-4));
    max-height: 70dvh;
    overflow: auto;
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    padding: var(--space-2);
    /* Named for exactly this case: chrome floating over content. */
    background: var(--surface-overlay);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    box-shadow: var(--shadow-lg);
    outline: none;
  }

  /* Only the height changes on a phone. This used to re-position the panel --
     `position: fixed` with left/right insets, to stop being anchored -- and it
     never worked: bits-ui positions `Popover.Content` through floating-ui,
     which writes `position`, `left` and `transform` as *inline* styles, and an
     inline style beats a stylesheet rule. The insets were ignored, the width
     collapsed, and the panel rendered as a ~40px vertical sliver under the
     bell on every screen below 768px.

     Nothing needs to replace them. The base rule is already responsive --
     `width: 24rem` capped by `max-width: calc(100vw - var(--space-4))` -- so a
     narrow viewport gets a panel just inside its edges, and floating-ui's
     collision handling, which is on by default, keeps it there. */
  @media (max-width: 768px) {
    :global(.ui-notification-panel) {
      max-height: 60dvh;
    }
  }

  .panel-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    flex-wrap: wrap;
    padding: 0 var(--space-2);
  }

  .panel-filters {
    display: flex;
    gap: var(--space-1);
  }

  .panel-list {
    display: flex;
    flex-direction: column;
  }

  .panel-msg {
    margin: 0;
  }

  .panel-foot {
    margin: 0;
    padding: 0 var(--space-2);
    border-top: 1px solid var(--border-subtle);
    padding-top: var(--space-2);
  }
</style>
