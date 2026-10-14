<!--
  The app's one loading indicator: a thin bar fixed to the top edge of the
  viewport, up while any `PageLoading` is mounted. Fixed so it stays put while
  the shell composes around it. Hidden from assistive tech, which reads the
  pane's own `PageLoading` label instead.
-->
<script lang="ts">
  import { pageLoading } from '$lib/stores/pageLoading';
</script>

{#if $pageLoading}
  <div class="page-loader" aria-hidden="true" data-testid="page-loader">
    <div class="page-loader-bar"></div>
  </div>
{/if}

<style>
  .page-loader {
    position: fixed;
    /* Under the status bar in the iOS shell and an installed app, on the app
       bar's top edge, rather than under the notch. */
    top: var(--safe-top);
    left: 0;
    right: 0;
    height: 2px;
    z-index: var(--z-toast);
    overflow: hidden;
    pointer-events: none;
    /* A load that finishes this fast never shows a bar at all. */
    opacity: 0;
    animation: page-loader-in 120ms ease-out 150ms forwards;
  }

  .page-loader-bar {
    width: 35%;
    height: 100%;
    background: var(--accent-blue);
    animation: page-loader-slide 1.1s ease-in-out infinite;
  }

  @keyframes page-loader-in {
    to {
      opacity: 1;
    }
  }

  @keyframes page-loader-slide {
    from {
      transform: translateX(-100%);
    }
    to {
      transform: translateX(300%);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .page-loader-bar {
      width: 100%;
      animation: none;
      opacity: 0.6;
    }
  }
</style>
