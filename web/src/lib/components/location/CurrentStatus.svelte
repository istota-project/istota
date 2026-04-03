<script lang="ts">
	import type { CurrentLocation } from '$lib/api';

	interface Props {
		current: CurrentLocation | null;
	}

	let { current }: Props = $props();

	function formatDuration(minutes: number | null): string {
		if (minutes == null) return '';
		if (minutes < 60) return `${minutes}m`;
		const h = Math.floor(minutes / 60);
		const m = minutes % 60;
		return m > 0 ? `${h}h ${m}m` : `${h}h`;
	}

	function formatBattery(level: number | null): string {
		if (level == null) return '';
		return `${Math.round(level * 100)}%`;
	}

	function timeAgo(timestamp: string): string {
		const diff = Date.now() - new Date(timestamp).getTime();
		const mins = Math.floor(diff / 60000);
		if (mins < 1) return 'just now';
		if (mins < 60) return `${mins}m ago`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h ago`;
		return `${Math.floor(hrs / 24)}d ago`;
	}
</script>

{#if current?.last_ping}
	<div class="status-card">
		<div class="status-place">
			{#if current.current_visit}
				<span class="place-name">{current.current_visit.place_name}</span>
				<span class="duration">{formatDuration(current.current_visit.duration_minutes)}</span>
			{:else if current.last_ping.place}
				<span class="place-name">{current.last_ping.place}</span>
			{:else}
				<span class="place-name dim">No place</span>
			{/if}
		</div>
		<div class="status-meta">
			<span class="timestamp">{timeAgo(current.last_ping.timestamp)}</span>
			{#if current.last_ping.battery != null}
				<span class="battery">{formatBattery(current.last_ping.battery)}</span>
			{/if}
		</div>
	</div>
{:else}
	<div class="status-card empty">No location data</div>
{/if}

<style>
	.status-card {
		background: var(--surface-card);
		border-radius: var(--radius-card);
		padding: 0.75rem 1rem;
	}

	.status-card.empty {
		color: var(--text-dim);
		font-size: var(--text-sm);
	}

	.status-place {
		display: flex;
		align-items: baseline;
		gap: 0.5rem;
	}

	.place-name {
		font-weight: 600;
		font-size: var(--text-base);
	}

	.place-name.dim {
		color: var(--text-dim);
		font-weight: 400;
	}

	.duration {
		font-size: var(--text-sm);
		color: var(--text-muted);
	}

	.status-meta {
		display: flex;
		gap: 0.75rem;
		margin-top: 0.25rem;
		font-size: var(--text-xs);
		color: var(--text-dim);
	}
</style>
