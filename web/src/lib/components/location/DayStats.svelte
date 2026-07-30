<script lang="ts">
  import type { LocationPing } from '$lib/api';

  interface Props {
    pings: LocationPing[];
  }

  let { pings }: Props = $props();

  function haversine(lat1: number, lon1: number, lat2: number, lon2: number): number {
    const R = 6371000;
    const toRad = (d: number) => (d * Math.PI) / 180;
    const dLat = toRad(lat2 - lat1);
    const dLon = toRad(lon2 - lon1);
    const a =
      Math.sin(dLat / 2) ** 2 +
      Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }

  let stats = $derived.by(() => {
    if (pings.length < 2) return null;

    let totalDist = 0;
    let maxSpeed = 0;

    for (let i = 1; i < pings.length; i++) {
      totalDist += haversine(pings[i - 1].lat, pings[i - 1].lon, pings[i].lat, pings[i].lon);
      const speed = pings[i].speed ?? 0;
      if (speed > maxSpeed) maxSpeed = speed;
    }

    const firstBattery = pings[0].battery;
    const lastBattery = pings[pings.length - 1].battery;
    const batteryDrain =
      firstBattery != null && lastBattery != null
        ? Math.round((firstBattery - lastBattery) * 100)
        : null;

    return {
      totalDist,
      maxSpeed,
      batteryDrain,
    };
  });

  function formatDist(m: number): string {
    return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`;
  }

  function formatSpeed(ms: number): string {
    return `${(ms * 3.6).toFixed(0)} km/h`;
  }
</script>

{#if stats}
  <div class="day-stats">
    <div class="stat-row">
      <span class="stat-label">Distance</span>
      <span class="stat-value">{formatDist(stats.totalDist)}</span>
    </div>
    {#if stats.maxSpeed > 0}
      <div class="stat-row">
        <span class="stat-label">Max speed</span>
        <span class="stat-value">{formatSpeed(stats.maxSpeed)}</span>
      </div>
    {/if}
    {#if stats.batteryDrain != null}
      <div class="stat-row">
        <span class="stat-label">Battery</span>
        <span class="stat-value"
          >{stats.batteryDrain > 0 ? `-${stats.batteryDrain}%` : `+${-stats.batteryDrain}%`}</span
        >
      </div>
    {/if}
  </div>
{/if}

<style>
  .day-stats {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .stat-row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
  }

  .stat-label {
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .stat-value {
    font-size: var(--text-xs);
    color: var(--text-primary);
    font-variant-numeric: tabular-nums;
  }
</style>
