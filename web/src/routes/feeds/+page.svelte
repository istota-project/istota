<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { getFeeds, updateEntriesStatus, type FeedEntry, type FeedsResponse } from '$lib/api';
	import FeedCard from '$lib/components/FeedCard.svelte';
	import Lightbox from '$lib/components/Lightbox.svelte';

	let data: FeedsResponse | null = $state(null);
	let loading = $state(true);
	let error = $state('');

	// Filters
	let showImages = $state(true);
	let showText = $state(true);
	let sortBy: 'published' | 'added' = $state('published');
	let viewMode: 'grid' | 'list' = $state('grid');

	// Lightbox
	let lightboxSrc = $state('');

	// Batch read queue
	const pendingReadIds = new Set<number>();
	let flushTimer: ReturnType<typeof setTimeout> | null = null;

	function flushPending() {
		if (pendingReadIds.size === 0) return;
		const ids = [...pendingReadIds];
		pendingReadIds.clear();
		flushTimer = null;
		updateEntriesStatus(ids, 'read').catch(() => {});
	}

	function handleViewed(id: number) {
		if (!data) return;
		const entry = data.entries.find((e) => e.id === id);
		if (entry && entry.status !== 'read') {
			entry.status = 'read';
		}
		pendingReadIds.add(id);
		if (flushTimer) clearTimeout(flushTimer);
		flushTimer = setTimeout(flushPending, 3000);
	}

	onMount(async () => {
		try {
			data = await getFeeds({ limit: '500', order: 'published_at', direction: 'desc' });
		} catch (e) {
			error = 'Failed to load feeds';
		} finally {
			loading = false;
		}
	});

	onDestroy(() => {
		if (flushTimer) clearTimeout(flushTimer);
		flushPending();
	});

	let filteredEntries = $derived.by(() => {
		if (!data) return [];
		let entries = data.entries.filter((e) => {
			const isImage = e.images.length > 0;
			if (isImage && !showImages) return false;
			if (!isImage && !showText) return false;
			return true;
		});
		entries.sort((a, b) => {
			const keyA = sortBy === 'published' ? a.published_at : a.created_at;
			const keyB = sortBy === 'published' ? b.published_at : b.created_at;
			return (keyB || '').localeCompare(keyA || '');
		});
		return entries;
	});

</script>

{#if loading}
	<div class="loading">Loading feeds...</div>
{:else if error}
	<div class="error-msg">{error}</div>
{:else if data}
	<nav class="filters">
		<div class="filter-type">
			<label class="filter-chip" class:checked={showImages}>
				<input type="checkbox" bind:checked={showImages} />
				<span>images</span>
			</label>
			<label class="filter-chip" class:checked={showText}>
				<input type="checkbox" bind:checked={showText} />
				<span>text</span>
			</label>
		</div>

		<div class="sort-toggle">
			<label class="filter-chip" class:checked={sortBy === 'published'}>
				<input type="radio" name="sort" value="published" bind:group={sortBy} />
				<span>published</span>
			</label>
			<label class="filter-chip" class:checked={sortBy === 'added'}>
				<input type="radio" name="sort" value="added" bind:group={sortBy} />
				<span>added</span>
			</label>
		</div>

		<div class="view-toggle">
			<label class="filter-chip" class:checked={viewMode === 'grid'}>
				<input type="radio" name="view" value="grid" bind:group={viewMode} />
				<span>grid</span>
			</label>
			<label class="filter-chip" class:checked={viewMode === 'list'}>
				<input type="radio" name="view" value="list" bind:group={viewMode} />
				<span>list</span>
			</label>
		</div>
	</nav>

	<div class="feed-grid" class:list-view={viewMode === 'list'}>
		{#each filteredEntries as entry (entry.id)}
			<FeedCard {entry} onImageClick={(url) => lightboxSrc = url} onViewed={handleViewed} />
		{/each}
	</div>

	<div class="status-notice">{data.total} items</div>

	<Lightbox src={lightboxSrc} onClose={() => lightboxSrc = ''} />
{/if}

<style>
	/* Filter bar */
	.filters {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		margin-bottom: 1.5rem;
		position: sticky;
		top: 0;
		z-index: 10;
		background: #111;
		padding: 0.75rem 0;
		align-items: center;
	}
	.filter-type {
		display: flex;
		gap: 0.5rem;
	}
	.sort-toggle, .view-toggle {
		display: flex;
		gap: 0.25rem;
	}
	.view-toggle { margin-left: auto; }

	/* Filter chips */
	.filter-chip {
		cursor: pointer;
		display: inline-flex;
		align-items: center;
		padding: 0.25rem 0.75rem;
		border: 1px solid #333;
		border-radius: 999px;
		font-size: 0.8rem;
		transition: all 0.15s;
		user-select: none;
	}
	.filter-chip input { display: none; }
	.filter-chip.checked {
		background: #e0e0e0;
		color: #111;
		border-color: #e0e0e0;
	}

	/* Grid layout */
	.feed-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
		gap: 1rem;
	}
	.feed-grid.list-view {
		grid-template-columns: 1fr;
		max-width: 640px;
		margin: 0 auto;
	}

	/* Cards */
	.feed-grid :global(.card) {
		position: relative;
		background: #1a1a1a;
		border-radius: 0.5rem;
		overflow: hidden;
		max-height: 420px;
		display: flex;
		flex-direction: column;
	}
	.feed-grid :global(.seen-pill) {
		position: absolute;
		top: 0.4rem;
		right: 0.4rem;
		font-size: 0.55rem;
		font-weight: 600;
		letter-spacing: 0.04em;
		padding: 0.1rem 0.35rem;
		background: rgba(0, 0, 0, 0.55);
		color: #888;
		border-radius: 0.2rem;
		pointer-events: none;
		z-index: 2;
	}
	.feed-grid.list-view :global(.card) {
		max-height: none;
	}

	/* Image cards */
	.feed-grid :global(.card-image) {
		display: flex;
		justify-content: center;
		cursor: zoom-in;
		border: none;
		padding: 0;
		background: #0e0e0e;
		width: 100%;
	}
	.feed-grid :global(.card-image img) {
		width: 100%;
		display: block;
		max-height: 360px;
		object-fit: contain;
		border-radius: 0.5rem 0.5rem 0 0;
	}

	/* Gallery */
	.feed-grid :global(.card-gallery) {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 2px;
	}
	.feed-grid :global(.card-gallery .card-image img) {
		border-radius: 0;
		aspect-ratio: 1;
		object-fit: cover;
		max-height: none;
	}
	.feed-grid :global(.card-gallery .card-image:first-child img) {
		border-radius: 0.5rem 0 0 0;
	}
	.feed-grid :global(.card-gallery .card-image:nth-child(2) img) {
		border-radius: 0 0.5rem 0 0;
	}
	.feed-grid :global(.card-gallery .card-image:only-child img) {
		border-radius: 0.5rem 0.5rem 0 0;
		grid-column: span 2;
		aspect-ratio: auto;
		object-fit: initial;
	}

	/* Gallery overflow */
	.feed-grid :global(.gallery-more) { position: relative; }
	.feed-grid :global(.gallery-count) {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		background: rgba(0, 0, 0, 0.55);
		color: #fff;
		font-size: 1.2rem;
		font-weight: 600;
		pointer-events: none;
	}

	/* Title overlay */
	.feed-grid :global(.card-title-overlay) {
		padding: 0.25rem 0.6rem;
		background: #161616;
		font-size: 0.7rem;
		color: #888;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.feed-grid :global(.card-title-overlay a) { color: #888; text-decoration: none; }
	.feed-grid :global(.card-title-overlay a:hover) { color: #ccc; }

	/* Card body */
	.feed-grid :global(.card-body) {
		flex: 1;
		min-height: 0;
		overflow: hidden;
	}
	.feed-grid :global(.card-body h3) {
		margin: 0;
		padding: 0.5rem 0.75rem 0.25rem;
		font-size: 0.8rem;
		font-weight: 600;
	}
	.feed-grid :global(.card-body h3 a) { color: #e0e0e0; text-decoration: none; }
	.feed-grid :global(.card-body h3 a:hover) { text-decoration: underline; }

	/* Excerpt */
	.feed-grid :global(.excerpt) {
		margin: 0;
		padding: 0.5rem 0.75rem;
		font-size: 0.85rem;
		color: #bbb;
	}
	.feed-grid :global(.excerpt a) { color: #aaa; text-decoration: underline; }
	.feed-grid :global(.excerpt a:hover) { color: #e0e0e0; }
	.feed-grid :global(.excerpt p) { margin: 0.5em 0; }
	.feed-grid :global(.excerpt img) {
		max-width: 100%;
		height: auto;
		border-radius: 0.25rem;
		margin: 0.5em 0;
		display: block;
	}

	/* Meta */
	.feed-grid :global(.meta) {
		display: flex;
		gap: 0.5rem;
		align-items: center;
		padding: 0.5rem 0.75rem;
		font-size: 0.75rem;
		color: #666;
		border-top: 1px solid #222;
		margin-top: auto;
	}
	.feed-grid :global(.feed-name) {
		background: #252525;
		padding: 0.1rem 0.4rem;
		border-radius: 0.2rem;
	}
	.feed-grid :global(.meta-link) {
		color: #666;
		text-decoration: none;
		margin-left: auto;
	}
	.feed-grid :global(.meta-link:hover) { color: #aaa; }

	/* Status */
	.status-notice {
		position: fixed;
		bottom: 0.75rem;
		right: 0.75rem;
		font-size: 0.7rem;
		color: #555;
		background: #161616;
		padding: 0.3rem 0.6rem;
		border-radius: 0.25rem;
		z-index: 5;
		pointer-events: none;
	}

	/* List view overrides */
	.feed-grid.list-view :global(.card-image img) {
		max-height: none;
		object-fit: cover;
		border-radius: 0;
	}
	.feed-grid.list-view :global(.card-gallery) {
		grid-template-columns: 1fr;
	}
	.feed-grid.list-view :global(.card-gallery .card-image img) {
		aspect-ratio: auto;
	}

	@media (max-width: 640px) {
		.filters { gap: 0.35rem; padding: 0.5rem 0; }
		.filter-type { gap: 0.35rem; }
		.sort-toggle, .view-toggle { gap: 0.15rem; }
		.filter-chip { font-size: 0.65rem; padding: 0.15rem 0.5rem; }
	}
</style>
