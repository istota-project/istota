<script lang="ts">
  import { PanelLeft, PanelLeftClose } from 'lucide-svelte';

  interface Props {
    open: boolean;
    label: string;
    count?: number;
    onclick: () => void;
  }

  let { open, label, count, onclick }: Props = $props();
</script>

<!--
  The drawer toggle, mobile only — above the breakpoint the sidebar is permanent
  and this hides itself. It belongs in the bar's *leading* slot, before the
  title (Material's navigation-icon slot, UIKit's nav-bar leading item).

  It replaced a handle pinned to the viewport's left edge, floating over the
  content. That pattern has to shout to be found, because it is tethered to no
  layout landmark — and shouting is exactly what made it compete with the
  content it sat on top of. Anchored in the bar, this one needs no chrome at all.
-->
<button
  class="sidebar-toggle"
  class:open
  {onclick}
  type="button"
  aria-expanded={open}
  aria-label={open ? `Close ${label}` : `Open ${label}${count !== undefined ? ` (${count})` : ''}`}
  title={open ? `Close ${label}` : `${label}${count !== undefined ? ` (${count})` : ''}`}
>
  <!-- A panel glyph reads "toggle the side panel"; a kebab would collide with
	     the overflow-menu meaning the room and feed rows already use. -->
  {#if open}<PanelLeftClose size={18} />{:else}<PanelLeft size={18} />{/if}
</button>

<style>
  .sidebar-toggle {
    display: none;
    font: inherit;
    cursor: pointer;
    align-items: center;
    justify-content: center;
  }

  @media (max-width: 768px) {
    .sidebar-toggle {
      display: inline-flex;
      /* Anchor for the hit-area pseudo below. */
      position: relative;
      /* The bar's height is set by its tallest child, which is the title's line
			   box (1.5rem). Matching it means adding this control can't make the bar
			   taller — the touch target is bought back by the ::before overlay, which
			   is out of flow and so costs no layout height. */
      width: 1.75rem;
      height: 1.5rem;
      padding: 0;
      background: none;
      border: none;
      border-radius: var(--radius-card);
      color: var(--text-muted);
      touch-action: manipulation;
      /* The button box is padding around a small glyph, so both margins are
			   negative: the leading one puts the *glyph* on the bar's left edge rather
			   than the box, and the trailing one eats most of the bar's gap so the
			   icon reads as attached to the title. The hit area keeps its full size
			   either way — only the box's contribution to layout shrinks. */
      margin-inline-start: -0.35rem;
      margin-inline-end: -0.15rem;
    }

    /* Touch target, decoupled from the visible box: out of flow, so it grows
		   the tappable area to ~44x48 without touching the bar's height. It bleeds
		   a little over the title, which is harmless — where a page makes the title
		   tappable, it drives this same toggle. */
    .sidebar-toggle::before {
      content: '';
      position: absolute;
      top: 50%;
      left: 50%;
      width: 2.5rem;
      height: 2.75rem;
      transform: translate(-50%, -50%);
    }

    .sidebar-toggle:hover {
      color: var(--text-primary);
      background: var(--surface-raised);
    }

    .sidebar-toggle:active {
      background: var(--surface-badge);
    }
  }
</style>
