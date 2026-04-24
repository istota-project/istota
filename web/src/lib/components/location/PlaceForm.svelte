<script lang="ts">
	import type { DiscoveredCluster, Place } from '$lib/api';

	interface Props {
		cluster?: DiscoveredCluster;
		place?: Place;
		onSave: (data: { name: string; lat: number; lon: number; radius_meters: number; category: string }) => void;
		onCancel: () => void;
	}

	let { cluster, place, onSave, onCancel }: Props = $props();

	const editing = $derived(!!place);

	let name = $state(place?.name ?? '');
	let category = $state(place?.category ?? 'other');
	let radius = $state(place?.radius_meters ?? cluster?.radius_meters ?? 100);
	let lat = $state(place?.lat ?? cluster?.lat ?? 0);
	let lon = $state(place?.lon ?? cluster?.lon ?? 0);

	const categories = [
		'home', 'work', 'gym', 'food', 'shopping',
		'social', 'friend', 'medical', 'hotel', 'transit', 'other',
	];

	function handleSave() {
		if (!name.trim()) return;
		onSave({
			name: name.trim(),
			lat,
			lon,
			radius_meters: radius,
			category,
		});
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') onCancel();
		if (e.key === 'Enter') handleSave();
	}
</script>

<svelte:window on:keydown={handleKeydown} />

<div class="overlay" onclick={onCancel} role="presentation">
	<div class="form-card" onclick={(e) => e.stopPropagation()} role="dialog">
		<div class="header">{editing ? 'Edit place' : 'Name this place'}</div>
		{#if cluster && !editing}
			<div class="meta">
				{cluster.total_pings} pings recorded here
			</div>
		{/if}

		<label class="field">
			<span>Name</span>
			<input type="text" bind:value={name} placeholder="e.g. Office, Gym..." autofocus />
		</label>

		<label class="field">
			<span>Category</span>
			<select bind:value={category}>
				{#each categories as cat}
					<option value={cat}>{cat[0].toUpperCase() + cat.slice(1)}</option>
				{/each}
			</select>
		</label>

		<label class="field">
			<span>Radius ({radius}m)</span>
			<input type="range" min="25" max="500" step="25" bind:value={radius} />
		</label>

		{#if editing}
			<div class="coords">
				<label class="field coord">
					<span>Lat</span>
					<input type="number" step="0.00001" bind:value={lat} />
				</label>
				<label class="field coord">
					<span>Lon</span>
					<input type="number" step="0.00001" bind:value={lon} />
				</label>
			</div>
		{/if}

		<div class="actions">
			<button class="btn cancel" onclick={onCancel} type="button">Cancel</button>
			<button class="btn save" onclick={handleSave} disabled={!name.trim()} type="button">
				{editing ? 'Update' : 'Save'}
			</button>
		</div>
	</div>
</div>

<style>
	.overlay {
		position: fixed;
		inset: 0;
		z-index: 100;
		background: rgba(0, 0, 0, 0.5);
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.form-card {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-card);
		padding: 1.25rem;
		width: 300px;
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}

	.header {
		font-size: var(--text-sm);
		font-weight: 600;
		color: var(--text-primary);
	}

	.meta {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}

	.field span {
		font-size: var(--text-xs);
		color: var(--text-muted);
	}

	.field input[type="text"],
	.field input[type="number"],
	.field select {
		background: var(--surface-bg);
		border: 1px solid var(--border-default);
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-sm);
		padding: 0.35rem 0.5rem;
		border-radius: 0.25rem;
	}

	.field input[type="range"] {
		accent-color: #ffc107;
	}

	.coords {
		display: flex;
		gap: 0.5rem;
	}

	.coord {
		flex: 1;
	}

	.coord input {
		width: 100%;
	}

	.actions {
		display: flex;
		gap: 0.5rem;
		justify-content: flex-end;
		margin-top: 0.25rem;
	}

	.btn {
		padding: 0.3rem 0.75rem;
		border: 1px solid var(--border-default);
		border-radius: 0.25rem;
		font: inherit;
		font-size: var(--text-xs);
		cursor: pointer;
		background: var(--surface-card);
		color: var(--text-primary);
	}

	.btn:hover { background: var(--surface-raised); }
	.btn.save { background: #ffc107; color: #111; border-color: #ffc107; }
	.btn.save:hover { background: #ffca28; }
	.btn.save:disabled { opacity: 0.4; cursor: default; }
</style>
