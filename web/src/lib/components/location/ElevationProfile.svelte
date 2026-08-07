<script lang="ts">
  import type { LocationPing } from '$lib/api';
  import {
    buildElevationPath,
    decimate,
    elevationPoints,
    elevationRange,
    hasMeaningfulElevation,
  } from '$lib/location-elevation';

  interface Props {
    pings: LocationPing[];
  }

  let { pings }: Props = $props();

  // The path is drawn in a fixed box and stretched to the strip's real width by
  // `preserveAspectRatio="none"`, so nothing here has to measure the DOM.
  const BOX_W = 100;
  const BOX_H = 40;

  // One pass over the pings: `points` feeds the gate, the readout and the path
  // alike. A day at the range view's cap is tens of thousands of pings, and
  // recomputing this per derived value cost a full scan each time.
  let points = $derived(elevationPoints(pings));
  let show = $derived(hasMeaningfulElevation(points));
  let range = $derived(show ? elevationRange(points) : null);
  let path = $derived(
    show ? buildElevationPath(decimate(points), BOX_W, BOX_H) : { solid: '', sparse: '' },
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
