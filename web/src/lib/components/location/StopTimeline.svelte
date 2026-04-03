<script lang="ts">
	import type { DaySummaryStop } from '$lib/api';

	interface Props {
		stops: DaySummaryStop[];
		onStopClick?: (stop: DaySummaryStop) => void;
	}

	let { stops, onStopClick }: Props = $props();

	function duration(arrived: string, departed: string): string {
		if (!arrived || !departed) return '';
		const [ah, am] = arrived.split(':').map(Number);
		const [dh, dm] = departed.split(':').map(Number);
		const mins = (dh * 60 + dm) - (ah * 60 + am);
		if (mins <= 0) return '';
		if (mins < 60) return `${mins}m`;
		const h = Math.floor(mins / 60);
		const m = mins % 60;
		return m > 0 ? `${h}h ${m}m` : `${h}h`;
	}
</script>

{#if stops.length > 0}
	<div class="timeline">
		{#each stops as stop, i}
			<button
				class="timeline-item"
				onclick={() => onStopClick?.(stop)}
				type="button"
			>
				<div class="timeline-dot-col">
					<div class="dot"></div>
					{#if i < stops.length - 1}
						<div class="line"></div>
					{/if}
				</div>
				<div class="timeline-content">
					<div class="stop-name">{stop.location}</div>
					<div class="stop-time">
						{stop.arrived}{#if stop.departed && stop.departed !== stop.arrived} &ndash; {stop.departed}{/if}
						{#if duration(stop.arrived, stop.departed)}
							<span class="stop-duration">{duration(stop.arrived, stop.departed)}</span>
						{/if}
					</div>
				</div>
			</button>
		{/each}
	</div>
{:else}
	<div class="empty">No stops today</div>
{/if}

<style>
	.timeline {
		display: flex;
		flex-direction: column;
	}

	.timeline-item {
		display: flex;
		gap: 0.75rem;
		background: none;
		border: none;
		color: inherit;
		font: inherit;
		cursor: pointer;
		text-align: left;
		padding: 0.25rem 0.5rem;
		border-radius: var(--radius-card);
		transition: background var(--transition-fast);
	}

	.timeline-item:hover {
		background: var(--surface-raised);
	}

	.timeline-dot-col {
		display: flex;
		flex-direction: column;
		align-items: center;
		padding-top: 0.35rem;
		width: 12px;
		flex-shrink: 0;
	}

	.dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: var(--text-primary);
		flex-shrink: 0;
	}

	.line {
		width: 1px;
		flex: 1;
		background: var(--border-default);
		margin-top: 4px;
		min-height: 16px;
	}

	.timeline-content {
		padding-bottom: 0.75rem;
		min-width: 0;
	}

	.stop-name {
		font-size: var(--text-base);
		font-weight: 500;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.stop-time {
		font-size: var(--text-sm);
		color: var(--text-muted);
		margin-top: 0.1rem;
	}

	.stop-duration {
		color: var(--text-dim);
		margin-left: 0.35rem;
	}

	.empty {
		color: var(--text-dim);
		font-size: var(--text-sm);
		padding: 0.5rem;
	}
</style>
