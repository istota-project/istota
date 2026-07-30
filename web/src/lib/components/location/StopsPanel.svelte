<script lang="ts">
  import type { DaySummary, DaySummaryStop, LocationPing } from '$lib/api';
  import type { Trip } from '$lib/location-path';
  import DayStats from './DayStats.svelte';
  import StopTimeline from './StopTimeline.svelte';
  import TripList from './TripList.svelte';

  interface Props {
    pings: LocationPing[];
    trips: Trip[];
    summary: DaySummary | null;
    onStopClick: (stop: DaySummaryStop) => void;
    onTripClick: (trip: Trip) => void;
  }

  let { pings, trips, summary, onStopClick, onTripClick }: Props = $props();
</script>

<!--
  The details drawer under the map's stats bar. /location and /location/history
  each carried this markup and its 45 lines of CSS verbatim — the map, the bar
  and the drawer are one arrangement, and only the page's own filters differ.
  The pages keep the `open` decision, which is genuinely different between
  them: today gates on having any detail at all, history on a single-day range.
-->
<div class="stops-panel">
  {#if pings.length > 1}
    <div class="panel-section">
      <DayStats {pings} />
    </div>
  {/if}
  {#if trips.length > 0}
    <div class="panel-section">
      <div class="panel-label">Trips</div>
      <TripList {trips} {onTripClick} />
    </div>
  {/if}
  {#if summary && summary.stops.length > 0}
    <div class="panel-section">
      <div class="panel-label">Stops</div>
      <StopTimeline stops={summary.stops} {onStopClick} />
    </div>
  {/if}
</div>

<style>
  .stops-panel {
    max-height: 200px;
    overflow-y: auto;
    border-top: 1px solid var(--border-subtle);
    padding: var(--space-2) var(--space-3);
    flex-shrink: 0;
  }

  .panel-section {
    padding-bottom: var(--space-2);
    margin-bottom: var(--space-1);
    border-bottom: 1px solid var(--border-subtle);
  }

  .panel-section:last-child {
    border-bottom: none;
    margin-bottom: 0;
    padding-bottom: 0;
  }

  .panel-label {
    font-size: var(--text-xs);
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-weight: 500;
    margin-bottom: var(--space-1);
  }
</style>
