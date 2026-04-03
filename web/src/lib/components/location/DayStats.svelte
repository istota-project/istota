<script lang="ts">
	import type { LocationPing } from '$lib/api';
	import { ACTIVITY_COLORS, ACTIVITY_LABELS } from '$lib/location-constants';

	interface Props {
		pings: LocationPing[];
	}

	let { pings }: Props = $props();

	function haversine(lat1: number, lon1: number, lat2: number, lon2: number): number {
		const R = 6371000;
		const toRad = (d: number) => d * Math.PI / 180;
		const dLat = toRad(lat2 - lat1);
		const dLon = toRad(lon2 - lon1);
		const a = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
		return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
	}

	let stats = $derived.by(() => {
		if (pings.length < 2) return null;

		let totalDist = 0;
		let maxSpeed = 0;
		const activityDuration: Record<string, number> = {};

		for (let i = 1; i < pings.length; i++) {
			totalDist += haversine(pings[i - 1].lat, pings[i - 1].lon, pings[i].lat, pings[i].lon);
			const speed = pings[i].speed ?? 0;
			if (speed > maxSpeed) maxSpeed = speed;

			const activity = pings[i].activity_type ?? 'stationary';
			const dt = (new Date(pings[i].timestamp).getTime() - new Date(pings[i - 1].timestamp).getTime()) / 1000;
			if (dt > 0 && dt < 600) { // skip gaps > 10 min
				activityDuration[activity] = (activityDuration[activity] ?? 0) + dt;
			}
		}

		const firstBattery = pings[0].battery;
		const lastBattery = pings[pings.length - 1].battery;
		const batteryDrain = (firstBattery != null && lastBattery != null)
			? Math.round((firstBattery - lastBattery) * 100)
			: null;

		return {
			totalDist,
			maxSpeed,
			batteryDrain,
			activityDuration: Object.entries(activityDuration)
				.sort(([, a], [, b]) => b - a),
		};
	});

	function formatDist(m: number): string {
		return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`;
	}

	function formatDuration(sec: number): string {
		if (sec < 60) return `${Math.round(sec)}s`;
		const m = Math.round(sec / 60);
		if (m < 60) return `${m}m`;
		const h = Math.floor(m / 60);
		return `${h}h ${m % 60}m`;
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
				<span class="stat-value">{stats.batteryDrain > 0 ? `-${stats.batteryDrain}%` : `+${-stats.batteryDrain}%`}</span>
			</div>
		{/if}
		{#if stats.activityDuration.length > 0}
			<div class="activity-breakdown">
				{#each stats.activityDuration as [activity, seconds]}
					<div class="activity-row">
						<span class="activity-dot" style="background: {ACTIVITY_COLORS[activity] ?? '#666'}"></span>
						<span class="activity-name">{ACTIVITY_LABELS[activity] ?? activity}</span>
						<span class="activity-dur">{formatDuration(seconds)}</span>
					</div>
				{/each}
			</div>
		{/if}
	</div>
{/if}

<style>
	.day-stats {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
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

	.activity-breakdown {
		margin-top: 0.15rem;
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}

	.activity-row {
		display: flex;
		align-items: center;
		gap: 0.3rem;
	}

	.activity-dot {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		flex-shrink: 0;
	}

	.activity-name {
		font-size: var(--text-xs);
		color: var(--text-muted);
		flex: 1;
	}

	.activity-dur {
		font-size: var(--text-xs);
		color: var(--text-primary);
		font-variant-numeric: tabular-nums;
	}
</style>
