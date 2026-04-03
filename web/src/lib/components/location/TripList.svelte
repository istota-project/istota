<script lang="ts">
	import type { Trip } from '$lib/api';
	import { ACTIVITY_COLORS, ACTIVITY_LABELS } from '$lib/location-constants';

	interface Props {
		trips: Trip[];
		onTripClick?: (trip: Trip) => void;
	}

	let { trips, onTripClick }: Props = $props();

	function formatTime(ts: string): string {
		const d = new Date(ts);
		return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
	}

	function formatDist(m: number): string {
		return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${m} m`;
	}

	function formatSpeed(ms: number): string {
		return `${(ms * 3.6).toFixed(0)} km/h`;
	}

	function tripDuration(trip: Trip): string {
		const ms = new Date(trip.end_time).getTime() - new Date(trip.start_time).getTime();
		const min = Math.round(ms / 60000);
		if (min < 60) return `${min}m`;
		return `${Math.floor(min / 60)}h ${min % 60}m`;
	}
</script>

{#if trips.length > 0}
	<div class="trip-list">
		{#each trips as trip}
			<button
				class="trip-item"
				onclick={() => onTripClick?.(trip)}
				type="button"
			>
				<span class="trip-dot" style="background: {ACTIVITY_COLORS[trip.activity_type] ?? '#4a9eff'}"></span>
				<div class="trip-info">
					<span class="trip-time">{formatTime(trip.start_time)} - {formatTime(trip.end_time)}</span>
					<span class="trip-details">
						{formatDist(trip.distance_m)}
						{#if trip.max_speed}&middot; {formatSpeed(trip.max_speed)}{/if}
						&middot; {tripDuration(trip)}
					</span>
				</div>
				<span class="trip-activity">{ACTIVITY_LABELS[trip.activity_type] ?? trip.activity_type}</span>
			</button>
		{/each}
	</div>
{/if}

<style>
	.trip-list {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}

	.trip-item {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		width: 100%;
		background: none;
		border: none;
		color: inherit;
		font: inherit;
		cursor: pointer;
		padding: 0.3rem 0.5rem;
		border-radius: 0.3rem;
		text-align: left;
		transition: background var(--transition-fast);
	}

	.trip-item:hover { background: var(--surface-raised); }

	.trip-dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		flex-shrink: 0;
	}

	.trip-info {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
	}

	.trip-time {
		font-size: var(--text-xs);
		color: var(--text-primary);
	}

	.trip-details {
		font-size: 0.65rem;
		color: var(--text-dim);
	}

	.trip-activity {
		font-size: var(--text-xs);
		color: var(--text-muted);
		flex-shrink: 0;
	}
</style>
