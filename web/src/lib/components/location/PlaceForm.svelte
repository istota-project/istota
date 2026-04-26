<script lang="ts">
	import { untrack } from 'svelte';
	import type { DiscoveredCluster, Place } from '$lib/api';
	import { Modal, Button, Select, type SelectOption } from '$lib/components/ui';

	interface Props {
		cluster?: DiscoveredCluster;
		place?: Place;
		initialLat?: number;
		initialLon?: number;
		onSave: (data: {
			name: string;
			lat: number;
			lon: number;
			radius_meters: number;
			category: string;
			notes: string;
		}) => void;
		onCancel: () => void;
		onDismiss?: (data: { lat: number; lon: number; radius_meters: number }) => void;
	}

	let { cluster, place, initialLat, initialLon, onSave, onCancel, onDismiss }: Props = $props();

	let nameInput: HTMLInputElement | undefined = $state();
	$effect(() => {
		nameInput?.focus();
	});

	const editing = $derived(!!place);
	const manual = $derived(!place && !cluster);
	const showCoords = $derived(editing || manual);

	let name = $state(untrack(() => place?.name ?? ''));
	let category = $state(untrack(() => place?.category ?? 'other'));
	let radius = $state(untrack(() => place?.radius_meters ?? cluster?.radius_meters ?? 100));
	let lat = $state(untrack(() => place?.lat ?? cluster?.lat ?? initialLat ?? 0));
	let lon = $state(untrack(() => place?.lon ?? cluster?.lon ?? initialLon ?? 0));
	let notes = $state(untrack(() => place?.notes ?? ''));
	let open = $state(true);

	const categoryOptions: SelectOption[] = [
		'home',
		'work',
		'gym',
		'food',
		'shopping',
		'social',
		'friend',
		'medical',
		'hotel',
		'transit',
		'other',
	].map((cat) => ({ value: cat, label: cat[0].toUpperCase() + cat.slice(1) }));

	function handleSave() {
		if (!name.trim()) return;
		onSave({
			name: name.trim(),
			lat,
			lon,
			radius_meters: radius,
			category,
			notes: notes.trim(),
		});
	}

	function handleDismiss() {
		onDismiss?.({ lat, lon, radius_meters: radius });
	}

	function handleOpenChange(next: boolean) {
		if (!next) onCancel();
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter') {
			const target = e.target as HTMLElement | null;
			if (target?.tagName === 'TEXTAREA') return;
			handleSave();
		}
	}

	const title = $derived(editing ? 'Edit place' : manual ? 'New place' : 'Name this place');
</script>

<svelte:window on:keydown={handleKeydown} />

<Modal bind:open {title} onOpenChange={handleOpenChange} width="320px">
	{#if cluster && !editing}
		<div class="meta">{cluster.total_pings} pings recorded here</div>
	{/if}

	<label class="field">
		<span>Name</span>
		<input
			type="text"
			bind:this={nameInput}
			bind:value={name}
			placeholder="e.g. Office, Gym..."
		/>
	</label>

	<label class="field">
		<span>Category</span>
		<Select bind:value={category} options={categoryOptions} ariaLabel="Category" />
	</label>

	<label class="field">
		<span>Radius ({radius}m)</span>
		<input type="range" min="25" max="500" step="25" bind:value={radius} />
	</label>

	{#if showCoords}
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

	<label class="field">
		<span>Notes (optional)</span>
		<textarea bind:value={notes} rows="2" placeholder="Anything to remember about this place"
		></textarea>
	</label>

	{#snippet footer()}
		<Button variant="ghost" onclick={onCancel}>Cancel</Button>
		{#if !editing && onDismiss}
			<Button variant="ghost" onclick={handleDismiss} title="Don't show this cluster again">
				Dismiss
			</Button>
		{/if}
		<Button variant="primary" onclick={handleSave} disabled={!name.trim()}>
			{editing ? 'Update' : 'Save'}
		</Button>
	{/snippet}
</Modal>

<style>
	.meta {
		font-size: var(--text-xs);
		color: var(--text-dim);
		margin-bottom: 0.5rem;
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		margin-bottom: 0.75rem;
	}

	.field span {
		font-size: var(--text-xs);
		color: var(--text-muted);
	}

	.field input[type='text'],
	.field input[type='number'],
	.field textarea {
		background: var(--surface-base);
		border: 1px solid var(--border-default);
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-sm);
		padding: 0.35rem 0.5rem;
		border-radius: 0.25rem;
	}

	.field textarea {
		resize: vertical;
		min-height: 2.5rem;
		font-family: inherit;
	}

	.field input[type='range'] {
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
</style>
