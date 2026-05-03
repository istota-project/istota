<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { getFeeds, type Feed } from '$lib/api';
	import {
		feedsList,
		selectedFeedId,
		showImages,
		showText,
		showUnseen,
		sortBy,
		viewMode,
	} from '$lib/stores/feeds';
	import {
		AppShell,
		ShellHeader,
		Sidebar,
		SidebarToggle,
		CategoryGroup,
		Chip,
	} from '$lib/components/ui';
	import { LayoutGrid, List, Cog } from 'lucide-svelte';

	let { children } = $props();

	let feeds: Feed[] = $state([]);
	let sidebarOpen = $state(false);

	let onSettings = $derived(page.url.pathname.startsWith(`${base}/feeds/settings`));

	function toggleSettings() {
		if (onSettings) goto(`${base}/feeds`);
		else goto(`${base}/feeds/settings`);
	}

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
		if (onSettings) goto(`${base}/feeds`);
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

<AppShell>
	{#snippet header()}
		<ShellHeader title="Feeds">
			{#snippet nav()}
				<div class="filter-group">
					<Chip checked={$showImages} onclick={() => showImages.update((v) => !v)}>Images</Chip>
					<Chip checked={$showText} onclick={() => showText.update((v) => !v)}>Text</Chip>
					<Chip checked={$showUnseen} onclick={() => showUnseen.update((v) => !v)}>Unseen</Chip>
				</div>
				<div class="filter-group">
					<Chip checked={$sortBy === 'published'} onclick={() => sortBy.set('published')}>Published</Chip>
					<Chip checked={$sortBy === 'added'} onclick={() => sortBy.set('added')}>Added</Chip>
				</div>
			{/snippet}
			{#snippet tools()}
				<div class="filter-group">
					<Chip icon checked={$viewMode === 'grid'} onclick={() => viewMode.set('grid')} title="Grid view">
						<LayoutGrid size={14} />
					</Chip>
					<Chip icon checked={$viewMode === 'list'} onclick={() => viewMode.set('list')} title="List view">
						<List size={14} />
					</Chip>
				</div>
				<Chip icon checked={onSettings} onclick={toggleSettings} title="Feed settings">
					<Cog size={14} />
				</Chip>
				<SidebarToggle
					open={sidebarOpen}
					label="Sources"
					count={feeds.length}
					onclick={() => (sidebarOpen = !sidebarOpen)}
				/>
			{/snippet}
		</ShellHeader>
	{/snippet}

	{#snippet sidebar()}
		<Sidebar
			title="Sources"
			count={feeds.length}
			open={sidebarOpen}
			onClose={() => (sidebarOpen = false)}
		>
			{#each groupedFeeds as [category, catFeeds] (category)}
				<CategoryGroup label={category} count={catFeeds.length} collapsible>
					{#each catFeeds as feed (feed.id)}
						<button
							class="feed-btn"
							class:active={$selectedFeedId === feed.id}
							onclick={() => handleFeedClick(feed.id)}
							type="button"
						>
							<span class="feed-name">{feed.title}</span>
						</button>
					{/each}
				</CategoryGroup>
			{/each}
		</Sidebar>
	{/snippet}

	{@render children()}
</AppShell>

<style>
	.filter-group {
		display: flex;
		gap: var(--chip-gap);
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
		padding: 0.3rem 0.75rem;
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

</style>
