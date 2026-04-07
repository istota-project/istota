<script lang="ts">
	import { onMount } from 'svelte';
	import { getFeeds, type Feed } from '$lib/api';
	import { feedsList, selectedFeedId, showImages, showText, showUnseen, sortBy, viewMode } from '$lib/stores/feeds';
	import Chip from '$lib/components/ui/Chip.svelte';
	import { LayoutGrid, List } from 'lucide-svelte';

	let { children } = $props();

	let feeds: Feed[] = $state([]);
	let sidebarOpen = $state(false);

	let groupedFeeds = $derived.by(() => {
		const groups: Record<string, Feed[]> = {};
		for (const f of feeds) {
			const cat = f.category.title || 'uncategorized';
			if (!groups[cat]) groups[cat] = [];
			groups[cat].push(f);
		}
		for (const arr of Object.values(groups)) {
			arr.sort((a, b) => a.title.localeCompare(b.title));
		}
		return Object.entries(groups).sort(([a], [b]) => a.localeCompare(b));
	});

	function handleFeedClick(feedId: number) {
		selectedFeedId.set($selectedFeedId === feedId ? 0 : feedId);
		sidebarOpen = false;
	}

	onMount(async () => {
		try {
			const data = await getFeeds({ limit: '1', offset: '0' });
			feeds = data.feeds;
			feedsList.set(data.feeds);
		} catch {
			// page handles its own errors
		}
	});
</script>

<div class="feed-shell">
	<div class="feed-header">
		<h1>Feeds</h1>
		<div class="feed-nav">
			<div class="filter-group">
				<Chip checked={$showImages} onclick={() => showImages.update(v => !v)}>Images</Chip>
				<Chip checked={$showText} onclick={() => showText.update(v => !v)}>Text</Chip>
				<Chip checked={$showUnseen} onclick={() => showUnseen.update(v => !v)}>Unseen</Chip>
			</div>
			<div class="filter-group">
				<Chip checked={$sortBy === 'published'} onclick={() => sortBy.set('published')}>Published</Chip>
				<Chip checked={$sortBy === 'added'} onclick={() => sortBy.set('added')}>Added</Chip>
			</div>
			<div class="filter-group view-toggle">
				<Chip icon checked={$viewMode === 'grid'} onclick={() => viewMode.set('grid')} title="Grid view">
					<LayoutGrid size={14} />
				</Chip>
				<Chip icon checked={$viewMode === 'list'} onclick={() => viewMode.set('list')} title="List view">
					<List size={14} />
				</Chip>
			</div>
		</div>
		<button class="sidebar-toggle" onclick={() => sidebarOpen = !sidebarOpen} type="button">
			{sidebarOpen ? 'Close' : 'Sources'} ({feeds.length})
		</button>
	</div>

	<div class="feed-body">
		<aside class="feed-sidebar" class:open={sidebarOpen}>
			<div class="sidebar-header">
				<span class="sidebar-title">Sources</span>
				<span class="sidebar-count">{feeds.length}</span>
			</div>
			<div class="sidebar-list">
				{#each groupedFeeds as [category, catFeeds]}
					<div class="cat-group">
						<div class="cat-label">{category}</div>
						{#each catFeeds as feed}
							<button
								class="feed-btn"
								class:active={$selectedFeedId === feed.id}
								onclick={() => handleFeedClick(feed.id)}
								type="button"
							>
								<span class="feed-name">{feed.title}</span>
							</button>
						{/each}
					</div>
				{/each}
			</div>
		</aside>

		<div class="feed-main">
			{@render children()}
		</div>
	</div>
</div>

<style>
	.feed-shell {
		display: flex;
		flex-direction: column;
		margin: -1.5rem;
		height: calc(100vh - 42px);
		overflow: hidden;
	}

	.feed-header {
		display: flex;
		align-items: baseline;
		gap: 1rem;
		padding: 0.75rem 1.5rem;
		border-bottom: 1px solid var(--border-subtle);
		flex-shrink: 0;
	}

	.feed-header h1 {
		font-size: 1rem;
		font-weight: 600;
		margin: 0;
	}

	.feed-nav {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		flex: 1;
		min-width: 0;
	}

	.filter-group {
		display: flex;
		gap: 0.25rem;
	}

	.filter-group.view-toggle {
		margin-left: auto;
	}

	.sidebar-toggle {
		display: none;
		margin-left: auto;
		background: var(--surface-card);
		border: none;
		color: var(--text-muted);
		font: inherit;
		font-size: var(--text-sm);
		padding: 0.25rem 0.6rem;
		border-radius: var(--radius-pill);
		cursor: pointer;
	}

	.feed-body {
		display: flex;
		flex: 1;
		min-height: 0;
	}

	.feed-sidebar {
		width: 200px;
		flex-shrink: 0;
		border-right: 1px solid var(--border-subtle);
		display: flex;
		flex-direction: column;
		overflow: hidden;
	}

	.sidebar-header {
		display: flex;
		align-items: baseline;
		gap: 0.4rem;
		padding: 0.6rem 1rem 0.6rem 1.5rem;
		flex-shrink: 0;
	}

	.sidebar-title {
		font-size: var(--text-sm);
		font-weight: 500;
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.sidebar-count {
		font-size: var(--text-xs);
		color: var(--text-dim);
	}

	.sidebar-list {
		flex: 1;
		overflow-y: auto;
		padding: 0 0.5rem 0.5rem;
	}

	.sidebar-list::-webkit-scrollbar { width: 4px; }
	.sidebar-list::-webkit-scrollbar-track { background: transparent; }
	.sidebar-list::-webkit-scrollbar-thumb { background: var(--border-default); border-radius: 2px; }

	.cat-group {
		margin-bottom: 0.25rem;
	}

	.cat-label {
		font-size: var(--text-xs);
		color: var(--text-dim);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		font-weight: 500;
		padding: 0.35rem 1rem 0.15rem;
	}

	.feed-btn {
		display: flex;
		align-items: center;
		width: 100%;
		background: none;
		border: none;
		color: inherit;
		font: inherit;
		cursor: pointer;
		padding: 0.3rem 1rem;
		border-radius: 0.3rem;
		transition: background var(--transition-fast);
		text-align: left;
	}

	.feed-btn:hover {
		background: var(--surface-raised);
	}

	.feed-btn.active {
		background: var(--surface-raised);
		color: var(--text-primary);
	}

	.feed-name {
		font-size: var(--text-sm);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.feed-main {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		overflow: hidden;
	}

	@media (max-width: 768px) {
		.feed-shell {
			margin: -1rem -0.75rem;
			height: calc(100vh - 36px);
		}

		.feed-header {
			padding: 0.5rem 0.75rem;
		}

		.feed-nav {
			gap: 0.35rem;
		}

		.filter-group {
			gap: 0.15rem;
		}

		.sidebar-toggle {
			display: block;
		}

		.feed-sidebar {
			display: none;
			position: absolute;
			top: 0;
			left: 0;
			bottom: 0;
			z-index: 20;
			width: 220px;
			background: var(--surface-base);
			border-right: 1px solid var(--border-default);
		}

		.feed-sidebar.open {
			display: flex;
		}

		.feed-body {
			position: relative;
		}
	}
</style>
