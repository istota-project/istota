<script lang="ts">
  import { Info, CircleCheck, TriangleAlert, CircleAlert, X } from 'lucide-svelte';
  import { currentNotice, dismissNotice, type NoticeSeverity } from '$lib/stores/notices';

  /**
   * The app's one transient-feedback surface: a band that slides down from
   * under the section header, overlays the content beneath it, and retracts on
   * dismiss or timeout.
   *
   * `AppShell` mounts it, which is what makes the anchor identical on every
   * view — every top-level route renders a shell, and the header is always the
   * band directly above the content. Anchoring here also keeps it clear of the
   * chat composer pinned to the bottom of the viewport, which is what ruled out
   * the usual bottom-corner toast.
   *
   * Two known gaps, both deliberate rather than overlooked:
   *
   * - Dismissal is the `×` only; the swipe-up gesture ISSUE-208 also names is
   *   not implemented. The band sits directly over a scrolling pane, so a
   *   vertical drag on it is ambiguous, and the `×` carries a full-size touch
   *   target.
   * - A route that renders no `AppShell` has no host, and the store's timers
   *   run regardless — so a notice raised in such a window can expire unseen.
   *   That covers the error page and the money layout's loading and
   *   load-failure branches. Nothing raises notices from any of them today;
   *   a surface that needs to must render a shell first.
   */

  const notice = $derived($currentNotice);

  const ICONS: Record<NoticeSeverity, typeof Info> = {
    info: Info,
    success: CircleCheck,
    warning: TriangleAlert,
    error: CircleAlert,
  };

  const LABELS: Record<NoticeSeverity, string> = {
    info: 'Note',
    success: 'Success',
    warning: 'Warning',
    error: 'Error',
  };

  const Icon = $derived(notice ? ICONS[notice.severity] : Info);

  /**
   * Announcement text, routed into whichever of the two regions below matches
   * the severity. An error interrupts; everything else waits for a pause.
   *
   * Both regions are mounted for the page's whole life and their `aria-live`
   * values never change. A region announces reliably only if assistive tech
   * had it registered, at that politeness, before the text arrived — so
   * flipping one region between `polite` and `assertive` as severities
   * alternate has the same defect as creating the region with its text.
   *
   * The count is deliberately left out: identical repeats coalesce to identical
   * announcement text, and unchanged text is not re-announced. Including it
   * would turn a burst that collapses to one visible band into one spoken
   * announcement per occurrence.
   */
  const announcement = $derived(notice ? `${LABELS[notice.severity]}: ${notice.message}` : '');
  const politeText = $derived(notice?.severity === 'error' ? '' : announcement);
  const assertiveText = $derived(notice?.severity === 'error' ? announcement : '');

  function motionDuration(): number {
    if (typeof window === 'undefined') return 0;
    return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 0 : 180;
  }

  /**
   * Animates one property and one only. Easing transform alongside opacity or a
   * colour is what let WebKit commit half a state change and leave the two
   * disagreeing (ISSUE-197, ISSUE-201); a single compositor-friendly property
   * has no second half to fall out of step with.
   */
  function slideDown(_node: Element, { duration }: { duration: number }) {
    return {
      duration,
      css: (t: number) => `transform: translateY(${(t - 1) * 100}%)`,
    };
  }

  /**
   * Both handlers read the live notice and bail when it has gone.
   *
   * Svelte pauses a block's render effects during its outro but leaves the
   * listeners live, so for the whole slide back up the panel is on screen,
   * clickable, and backed by a store that is already empty. Anything the
   * handler dereferences there is null — including an `{@const}` captured in
   * the block, which compiles to a derived and simply recomputes. Guarding is
   * the fix, and it is also the right behaviour: a notice that has been
   * answered should not be answerable a second time.
   */
  function dismissCurrent() {
    if (!notice) return;
    dismissNotice(notice.id);
  }

  function takeAction() {
    const current = notice;
    if (!current) return;
    // Dismiss first: the notice has been answered, and leaving it up invites a
    // second press of a button whose work is already under way.
    dismissNotice(current.id);
    current.action?.run();
  }

  function countLabel(count: number): string {
    // A flapping poller can coalesce into the thousands, and the band is a
    // single row also holding the message and up to two controls.
    return count > 99 ? '×99+' : `×${count}`;
  }
</script>

<div class="notice-region" data-testid="notice-region">
  {#if notice}
    <div
      class="notice-panel"
      data-testid="notice-panel"
      data-severity={notice.severity}
      transition:slideDown={{ duration: motionDuration() }}
    >
      <span class="notice-icon" data-testid="notice-icon" aria-hidden="true">
        <Icon size={16} />
      </span>
      <p class="notice-message">
        <span class="notice-severity-label">{LABELS[notice.severity]}:</span>
        {notice.message}
        {#if notice.count > 1}
          <span class="notice-count" data-testid="notice-count">{countLabel(notice.count)}</span>
        {/if}
      </p>
      {#if notice.action}
        <button
          type="button"
          class="notice-action"
          data-testid="notice-action"
          onclick={takeAction}
        >
          {notice.action.label}
        </button>
      {/if}
      <button
        type="button"
        class="notice-dismiss"
        aria-label="Dismiss notification"
        onclick={dismissCurrent}
      >
        <X size={15} />
      </button>
    </div>
  {/if}
</div>

<span class="notice-announce" data-testid="notice-announce-polite" aria-live="polite"
  >{politeText}</span
>
<span class="notice-announce" data-testid="notice-announce-assertive" aria-live="assertive"
  >{assertiveText}</span
>

<style>
  /* Absolute rather than in flow, so the band draws over the content instead of
     pushing it down — a notice that reflowed the page would move whatever the
     user was reading or aiming at, twice. Positioned against `.shell-header`,
     which sits inside the shell's horizontal safe-area padding, so a landscape
     notch is already cleared and needs no inset of its own here.

     `overflow: hidden` is what the slide is clipped by, and it clips hit
     testing too — so the panel cannot be left invisible-but-tappable partway
     through the animation (the ISSUE-197 failure).

     Above the mobile sidebar drawer and its backdrop (20 / 19 in Sidebar):
     both resolve in the same stacking context as this, and an equal z-index
     would hand the overlap to whichever comes later in the tree, which is the
     drawer. */
  .notice-region {
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    overflow: hidden;
    z-index: 30;
    pointer-events: none;
  }

  .notice-panel {
    pointer-events: auto;
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.55rem 0.75rem;
    background: var(--notice-bg);
    color: var(--notice-fg);
    border-bottom: 1px solid var(--border-subtle);
    font-size: var(--text-sm);
    box-shadow: var(--shadow-overlay);
  }

  /* The filled-chip pairing: the severity's `-bg` as the fill, its `-fg` as the
     text laid on it. Both are defined in each theme block, so the band reads in
     light and dark without a per-theme override here. */
  .notice-panel[data-severity='info'] {
    --notice-bg: var(--status-info-bg);
    --notice-fg: var(--status-info-fg);
  }
  .notice-panel[data-severity='success'] {
    --notice-bg: var(--status-success-bg);
    --notice-fg: var(--status-success-fg);
  }
  .notice-panel[data-severity='warning'] {
    --notice-bg: var(--status-warn-bg);
    --notice-fg: var(--status-warn-fg);
  }
  .notice-panel[data-severity='error'] {
    --notice-bg: var(--status-danger-bg);
    --notice-fg: var(--status-danger-fg);
  }

  .notice-icon {
    display: inline-flex;
    flex-shrink: 0;
  }

  .notice-message {
    margin: 0;
    min-width: 0;
    flex: 1;
  }

  /* Carries the severity to a screen reader that navigates to the band, since
     the icon is decorative and colour alone cannot be the signal. */
  .notice-severity-label,
  .notice-announce {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
    border: 0;
  }

  .notice-count {
    opacity: 0.75;
    font-variant-numeric: tabular-nums;
  }

  .notice-action {
    font: inherit;
    font-weight: 600;
    flex-shrink: 0;
    background: none;
    border: 1px solid currentColor;
    border-radius: var(--radius-pill);
    color: inherit;
    padding: 0.15rem 0.7rem;
    cursor: pointer;
  }

  /* Sized in em against the inherited font so it tracks the text-scale
     preference, with the touch target added out of flow — a 44px-tall control
     here would make the band taller than the text needs. */
  .notice-dismiss {
    font: inherit;
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    width: 1.5em;
    height: 1.5em;
    padding: 0;
    background: none;
    border: none;
    border-radius: 50%;
    color: inherit;
    opacity: 0.75;
    cursor: pointer;
  }

  .notice-dismiss::before {
    content: '';
    position: absolute;
    top: 50%;
    left: 50%;
    width: 44px;
    height: 44px;
    transform: translate(-50%, -50%);
  }

  .notice-dismiss:hover {
    opacity: 1;
  }
</style>
