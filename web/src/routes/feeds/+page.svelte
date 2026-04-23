<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { getFeeds, updateEntriesStatus, type FeedEntry } from '$lib/api';
	import { selectedFeedId, showImages, showText, showUnseen, sortBy, viewMode } from '$lib/stores/feeds';
	import FeedCard from '$lib/components/FeedCard.svelte';
	import Lightbox from '$lib/components/Lightbox.svelte';

	const PAGE_SIZE = 50;

	let entries: FeedEntry[] = $state([]);
	let total = $state(0);
	let loading = $state(true);
	let loadingMore = $state(false);
	let error = $state('');
	let hasMore = $state(true);

	// Watch for unseen toggle — reload with/without server-side status filter
	let prevSu = false;
	$effect(() => {
		if ($showUnseen !== prevSu) {
			loadEntries($selectedFeedId);
		}
		prevSu = $showUnseen;
	});

	// Lightbox
	let lightboxSrc = $state('');

	// Batch read queue
	const pendingReadIds = new Set<number>();
	let flushTimer: ReturnType<typeof setTimeout> | null = null;
	let flushMaxTimer: ReturnType<typeof setTimeout> | null = null;

	function flushPending() {
		if (pendingReadIds.size === 0) return;
		const ids = [...pendingReadIds];
		pendingReadIds.clear();
		if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; }
		if (flushMaxTimer) { clearTimeout(flushMaxTimer); flushMaxTimer = null; }
		updateEntriesStatus(ids, 'read').catch(() => {});
	}

	function handleViewed(id: number) {
		const entry = entries.find((e) => e.id === id);
		if (entry && entry.status !== 'read') {
			entry.status = 'read';
		}
		pendingReadIds.add(id);
		if (flushTimer) clearTimeout(flushTimer);
		flushTimer = setTimeout(flushPending, 3000);
		if (!flushMaxTimer) {
			flushMaxTimer = setTimeout(flushPending, 10000);
		}
	}

	type PageOpts = { offset?: number; before?: number; feedId?: number };

	async function loadPage(opts: PageOpts) {
		const params: Record<string, string> = {
			limit: String(PAGE_SIZE),
			order: 'published_at',
			direction: 'desc',
		};
		if (opts.feedId) params.feed_id = String(opts.feedId);
		if ($showUnseen) params.status = 'unread';
		if (opts.before != null) {
			params.before = String(opts.before);
			params.offset = '0';
		} else {
			params.offset = String(opts.offset ?? 0);
		}
		return await getFeeds(params);
	}

	async function loadEntries(feedId: number) {
		loading = true;
		error = '';
		try {
			const data = await loadPage({ offset: 0, feedId });
			entries = data.entries;
			total = data.total;
			hasMore = entries.length < total;
		} catch {
			error = 'Failed to load feeds';
		} finally {
			loading = false;
		}
		// Scroll to top on reload
		scrollRoot?.scrollTo(0, 0);
	}

	async function loadMore() {
		if (loadingMore || !hasMore) return;
		loadingMore = true;
		try {
			// Under the unread filter, offset is unstable: cards get marked read
			// mid-scroll, shrinking the server's unread pool and shifting offsets.
			// Use a `before` cursor on published_at instead.
			let opts: PageOpts;
			if ($showUnseen && entries.length > 0) {
				const oldest = entries[entries.length - 1];
				const oldestTs = oldest.published_at
					? Math.floor(new Date(oldest.published_at).getTime() / 1000)
					: 0;
				if (!oldestTs) {
					hasMore = false;
					return;
				}
				// +1 to include entries at exactly oldestTs (Miniflux `before` is
				// strictly less-than); dedup drops any already-loaded overlap.
				opts = { before: oldestTs + 1, feedId: $selectedFeedId };
			} else {
				opts = { offset: entries.length, feedId: $selectedFeedId };
			}

			const data = await loadPage(opts);
			if (data.entries.length === 0) {
				hasMore = false;
			} else {
				const seen = new Set(entries.map((e) => e.id));
				const fresh = data.entries.filter((e) => !seen.has(e.id));
				if (fresh.length === 0) {
					hasMore = false;
				} else {
					entries = [...entries, ...fresh];
					// Under cursor mode miniflux returns total-matching-filter,
					// which shrinks as we paginate; keep the initial total.
					if (!$showUnseen) total = data.total;
					hasMore = data.entries.length >= PAGE_SIZE;
				}
			}
		} catch {
			hasMore = false;
		} finally {
			loadingMore = false;
		}
		// Re-observe sentinel so the observer fires again if it's still visible
		// (needed when client-side filters hide most entries and the sentinel
		// never leaves the viewport between loads)
		if (hasMore && sentinel && scrollObserver) {
			scrollObserver.unobserve(sentinel);
			scrollObserver.observe(sentinel);
		}
	}

	// Infinite scroll sentinel
	let sentinel: HTMLDivElement | undefined = $state();
	let scrollObserver: IntersectionObserver | null = null;
	let scrollRoot: HTMLDivElement | undefined = $state();

	// Reload when selected feed changes
	let prevSelFeed: number | null = null;
	$effect(() => {
		if (prevSelFeed !== null && $selectedFeedId !== prevSelFeed) {
			loadEntries($selectedFeedId);
		}
		prevSelFeed = $selectedFeedId;
	});

	onMount(() => loadEntries($selectedFeedId));

	$effect(() => {
		if (!sentinel) return;
		scrollObserver?.disconnect();
		scrollObserver = new IntersectionObserver(
			(observed) => {
				if (observed[0].isIntersecting) loadMore();
			},
			{ root: scrollRoot, rootMargin: '600px' },
		);
		scrollObserver.observe(sentinel);
		return () => scrollObserver?.disconnect();
	});

	onDestroy(() => {
		if (flushTimer) clearTimeout(flushTimer);
		if (flushMaxTimer) clearTimeout(flushMaxTimer);
		flushPending();
	});

	let filteredEntries = $derived.by(() => {
		let filtered = entries.filter((e) => {
			const isImage = e.images.length > 0;
			if (isImage && !$showImages) return false;
			if (!isImage && !$showText) return false;
			return true;
		});
		filtered.sort((a, b) => {
			const keyA = $sortBy === 'published' ? a.published_at : a.created_at;
			const keyB = $sortBy === 'published' ? b.published_at : b.created_at;
			return (keyB || '').localeCompare(keyA || '');
		});
		return filtered;
	});
</script>

<div class="feed-scroll" bind:this={scrollRoot}>
	{#if loading}
		<div class="center-msg">Loading feeds...</div>
	{:else if error}
		<div class="center-msg error">{error}</div>
	{:else}
		<div class="feed-grid" class:list-view={$viewMode === 'list'}>
			{#each filteredEntries as entry (entry.id)}
				<FeedCard {entry} onImageClick={(url) => lightboxSrc = url} onViewed={handleViewed} />
			{/each}
		</div>

		<div bind:this={sentinel} class="sentinel">
			{#if loadingMore}
				<span class="loading-more">Loading more...</span>
			{/if}
		</div>
	{/if}
</div>

{#if !loading && !error}
	<div class="status-badge">{entries.length} / {total}</div>
{/if}

<Lightbox src={lightboxSrc} onClose={() => lightboxSrc = ''} />

<style>
	.center-msg {
		min-height: 100%;
		display: flex;
		align-items: center;
		justify-content: center;
		color: var(--text-dim);
		font-size: var(--text-sm);
	}

	.center-msg.error { color: #c66; }

	.feed-scroll {
		flex: 1;
		overflow-y: auto;
		padding: 0.75rem;
	}

	.feed-scroll::-webkit-scrollbar { width: 4px; }
	.feed-scroll::-webkit-scrollbar-track { background: transparent; }
	.feed-scroll::-webkit-scrollbar-thumb { background: var(--border-default); border-radius: 2px; }

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
		background: var(--surface-card);
		border-radius: var(--radius-card);
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
		color: var(--text-muted);
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
		border-radius: var(--radius-card) var(--radius-card) 0 0;
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
		border-radius: var(--radius-card) 0 0 0;
	}

	.feed-grid :global(.card-gallery .card-image:nth-child(2) img) {
		border-radius: 0 var(--radius-card) 0 0;
	}

	.feed-grid :global(.card-gallery .card-image:only-child img) {
		border-radius: var(--radius-card) var(--radius-card) 0 0;
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
		font-size: var(--text-xs);
		color: var(--text-muted);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.feed-grid :global(.card-title-overlay a) { color: var(--text-muted); text-decoration: none; }
	.feed-grid :global(.card-title-overlay a:hover) { color: var(--text-secondary); }

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

	.feed-grid :global(.card-body h3 a) { color: var(--text-primary); text-decoration: none; }
	.feed-grid :global(.card-body h3 a:hover) { text-decoration: underline; }

	/* Excerpt */
	.feed-grid :global(.excerpt) {
		margin: 0;
		padding: 0.5rem 0.75rem;
		font-size: var(--text-base);
		color: var(--text-secondary);
	}

	.feed-grid :global(.excerpt a) { color: #aaa; text-decoration: underline; }
	.feed-grid :global(.excerpt a:hover) { color: var(--text-primary); }
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
		font-size: var(--text-sm);
		color: var(--text-dim);
		border-top: 1px solid var(--border-subtle);
		margin-top: auto;
	}

	.feed-grid :global(.feed-name) {
		background: var(--surface-badge);
		padding: 0.1rem 0.4rem;
		border-radius: 0.2rem;
	}

	.feed-grid :global(.meta-link) {
		color: var(--text-dim);
		text-decoration: none;
		margin-left: auto;
	}

	.feed-grid :global(.meta-link:hover) { color: #aaa; }

	/* Sentinel / loading */
	.sentinel {
		height: 1px;
		text-align: center;
		padding: 1rem 0;
	}

	.loading-more {
		font-size: var(--text-sm);
		color: var(--text-dim);
	}

	/* Status badge */
	.status-badge {
		position: fixed;
		bottom: 0.75rem;
		right: 0.75rem;
		font-size: var(--text-xs);
		color: var(--text-dim);
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
</style>
