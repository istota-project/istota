<script lang="ts">
  /**
   * The app nav's notification control: a `.nav-icon-btn` carrying a bell and
   * the open count, opening `NotificationPanel`.
   *
   * **The badge is the count of open rows.** One number, no dot, no second
   * concept — every open row means the same thing, that something is waiting on
   * you, whether it is waiting to be done or waiting to be looked at. `seen_at`
   * is deliberately not what drives it: an object-backed item you have seen and
   * not acted on still needs you.
   *
   * The accessible name carries the number, because the pill is a two-character
   * glyph next to an icon and a screen reader given "Notifications" alone would
   * be told nothing the sighted user is being told.
   *
   * Layout, hit area, reset and hover all come from the shared `.nav-icon-btn`
   * rule in `app-shell.css`, like its three siblings in the nav — only the
   * resting colour is set here. The row's ~44px touch overlays are sized against
   * a fixed 1.25rem gap in `markdown.css`; a fourth control does not change that
   * pitch, so nothing there needs re-deriving.
   */
  import { Bell } from 'lucide-svelte';
  import { notificationCounts } from '$lib/stores/notifications';
  import CountPill from './CountPill.svelte';
  import NotificationPanel from './NotificationPanel.svelte';

  const open = $derived($notificationCounts.open);
  // The real number, not the pill's truncated `99+` — a screen reader has no
  // reason to be told the abbreviation the glyph had to fit into.
  const label = $derived(open === 0 ? 'Notifications' : `Notifications, ${open} waiting`);
</script>

<NotificationPanel>
  {#snippet trigger(props)}
    <button
      {...props}
      type="button"
      class="nav-icon-btn notification-btn"
      title="Notifications"
      aria-label={label}
    >
      <Bell size={15} />
      <CountPill count={open} {label} />
    </button>
  {/snippet}
</NotificationPanel>

<style>
  /* Everything but the resting colour is the shared `.nav-icon-btn` rule. */
  .notification-btn {
    color: var(--text-dim);
    /* The pill overlaps the glyph's top-right corner rather than sitting beside
       it: `.nav-right` is a fixed-gap row of icon controls, and a control that
       widens with its own count would shove its neighbours along the row every
       time a notification arrived — the same reasoning that put `minChars` on
       the money year filter. */
    position: relative;
  }

  .notification-btn :global(.count-pill) {
    position: absolute;
    top: 0;
    left: 55%;
  }
</style>
