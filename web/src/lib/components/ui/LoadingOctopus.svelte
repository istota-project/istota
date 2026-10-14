<!--
  A loading state drawn as the octopus sigil, pulsing gently, for a surface
  that would otherwise show a bare "Loading…" (the chat transcript). The label
  is hidden text for a screen reader. It fades in after 150ms, as `PageLoader` does,
  so a load that finishes quickly shows nothing.
-->
<script lang="ts">
  import { base } from '$app/paths';

  let { label = 'Loading…' }: { label?: string } = $props();
</script>

<div class="loading-octopus">
  <span class="loading-octopus-mark" style:--sigil-url="url('{base}/octopus-sigil.webp')"></span>
  <span class="visually-hidden">{label}</span>
</div>

<style>
  .loading-octopus {
    display: flex;
    align-items: center;
    justify-content: center;
    opacity: 0;
    animation: octopus-in 200ms ease-out 150ms forwards;
  }

  /* The sigil's shape filled with a mid grey, through a mask rather than the
     image itself: the image is near-white and `--sigil-filter` inverts it to
     near-black on the light theme, and both read too strong for a wait. */
  .loading-octopus-mark {
    display: block;
    width: 2.4em;
    aspect-ratio: 19 / 20;
    background-color: var(--text-dim);
    mask: var(--sigil-url) center / contain no-repeat;
    animation: octopus-pulse 1.8s ease-in-out infinite;
  }

  @keyframes octopus-in {
    to {
      opacity: 1;
    }
  }

  /* Opacity only: the sigil keeps its shape and place. */
  @keyframes octopus-pulse {
    0%,
    100% {
      opacity: 0.5;
    }
    50% {
      opacity: 1;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .loading-octopus-mark {
      animation: none;
      opacity: 0.75;
    }
  }
</style>
