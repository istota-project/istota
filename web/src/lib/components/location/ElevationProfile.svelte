<script lang="ts">
  import { buildElevationPath, decimate, type ElevationSummary } from '$lib/location-elevation';

  interface Props {
    /** Computed by the page, which needs the same answer for its disclosure
     *  toggle and its stats bar — see `elevationSummary`. */
    summary: ElevationSummary;
  }

  let { summary }: Props = $props();

  // The path is drawn in a fixed box and stretched to the strip's real width by
  // `preserveAspectRatio="none"`, so nothing here has to measure the DOM.
  const BOX_W = 100;
  const BOX_H = 40;

  let show = $derived(summary.show);
  let range = $derived(summary.range);
  let path = $derived(
    show ? buildElevationPath(decimate(summary.points), BOX_W, BOX_H) : { solid: '', sparse: '' },
  );

  function metres(n: number): string {
    return `${Math.round(n)} m`;
  }
</script>

{#if show && range}
  <div class="elevation">
    <div class="elevation-head">
      <span class="micro-label">Elevation</span>
      <span class="elevation-range">{metres(range.min)} – {metres(range.max)}</span>
    </div>
    <svg
      class="elevation-plot"
      viewBox="0 0 {BOX_W} {BOX_H}"
      preserveAspectRatio="none"
      role="img"
      aria-label="Elevation profile: {metres(range.min)} to {metres(range.max)}"
    >
      <!-- vector-effect keeps the stroke even under the non-uniform scale the
           viewBox stretch applies. The dashed path spans stretches the device
           never sampled, matching how the map dashes the same gaps. -->
      {#if path.sparse}
        <path class="gap" d={path.sparse} fill="none" vector-effect="non-scaling-stroke" />
      {/if}
      <path d={path.solid} fill="none" vector-effect="non-scaling-stroke" />
    </svg>
    <p class="caption">
      Device-reported altitude. Accuracy and reference vary by source — not an altimeter.
    </p>
  </div>
{/if}

<style>
  .elevation {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    padding: var(--space-2) var(--space-3);
    border-top: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }

  .elevation-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: var(--space-2);
  }

  .elevation-range {
    font-size: var(--text-xs);
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  .elevation-plot {
    width: 100%;
    height: 3rem;
    display: block;
  }

  .elevation-plot path {
    stroke: var(--accent-blue);
    stroke-width: 1.5px;
    stroke-linejoin: round;
    stroke-linecap: round;
  }

  .elevation-plot path.gap {
    stroke: var(--text-dim);
    stroke-dasharray: 3 3;
  }

  .caption {
    margin: 0;
  }
</style>
