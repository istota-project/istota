<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { getFeeds, markAsRead, type Feed } from '$lib/api';
	import {
		feedsList,
		selectedFeedId,
		showImages,
		showStarred,
		showText,
		showUnseen,
		sortBy,
		viewMode,
		feedsRefreshNonce,
	} from '$lib/stores/feeds';
	import {
		AppShell,
		ShellHeader,
		Sidebar,
		SidebarToggle,
		CategoryGroup,
		Chip,
	} from '$lib/components/ui';
	import { LayoutGrid, List, Cog, Star, CheckCheck, Circle } from 'lucide-svelte';

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
		showStarred.set(false);
		showUnseen.set(false);
		sidebarOpen = false;
		if (onSettings) goto(`${base}/feeds`);
	}

	function handleAllClick() {
		selectedFeedId.set(0);
		showStarred.set(false);
		showUnseen.set(false);
		sidebarOpen = false;
		if (onSettings) goto(`${base}/feeds`);
	}

	function handleUnreadClick() {
		showUnseen.set(true);
		showStarred.set(false);
		selectedFeedId.set(0);
		sidebarOpen = false;
		if (onSettings) goto(`${base}/feeds`);
	}

	function handleStarredClick() {
		showStarred.set(true);
		showUnseen.set(false);
		selectedFeedId.set(0);
		sidebarOpen = false;
		if (onSettings) goto(`${base}/feeds`);
	}

	async function handleMarkAllRead() {
		const scope = $selectedFeedId ? 'feed' : 'all';
		const targetTitle = $selectedFeedId
			? feeds.find((f) => f.id === $selectedFeedId)?.title || 'this feed'
			: 'every feed';
		const confirmed = window.confirm(
			`Mark all unread entries in ${targetTitle} as read? This can't be undone.`,
		);
		if (!confirmed) return;
		try {
			await markAsRead(scope, $selectedFeedId ? { id: $selectedFeedId } : undefined);
			feedsRefreshNonce.update((n) => n + 1);
		} catch (e) {
			console.error('mark-all-read failed', e);
		}
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
				<label class="sort-by">
					<span>Sort by</span>
					<select class="mode-select" bind:value={$sortBy}>
						<option value="published">Published</option>
						<option value="added">Added</option>
					</select>
				</label>
				<div class="filter-group">
					<Chip checked={$showImages} onclick={() => showImages.update((v) => !v)}>Images</Chip>
					<Chip checked={$showText} onclick={() => showText.update((v) => !v)}>Text</Chip>
				</div>
			{/snippet}
			{#snippet tools()}
				<Chip icon checked={$viewMode === 'grid'} onclick={() => viewMode.set('grid')} title="Grid view">
					<LayoutGrid size={14} />
				</Chip>
				<Chip icon checked={$viewMode === 'list'} onclick={() => viewMode.set('list')} title="List view">
					<List size={14} />
				</Chip>
				<Chip icon onclick={handleMarkAllRead}
					title={$selectedFeedId
						? 'Mark this feed as read'
						: 'Mark every feed as read'}>
					<CheckCheck size={14} />
				</Chip>
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
			<button
				class="feed-btn special"
				class:active={!$selectedFeedId && !$showStarred && !$showUnseen}
				onclick={handleAllClick}
				type="button"
			>
				<span class="feed-name">All</span>
			</button>
			<button
				class="feed-btn special"
				class:active={$showUnseen && !$showStarred && !$selectedFeedId}
				onclick={handleUnreadClick}
				type="button"
			>
				<Circle size={12} />
				<span class="feed-name">Unread</span>
			</button>
			<button
				class="feed-btn special"
				class:active={$showStarred}
				onclick={handleStarredClick}
				type="button"
			>
				<Star size={12} />
				<span class="feed-name">Starred</span>
			</button>
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

	.sort-by {
		display: inline-flex;
		align-items: center;
		gap: 0.4rem;
		font-size: var(--text-xs);
		color: var(--text-muted);
	}

	.mode-select {
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-xs);
		padding: 0.2rem 0.4rem;
		border-radius: 0.25rem;
		cursor: pointer;
	}

	.feed-btn {
		display: flex;
		align-items: center;
		gap: 0.4rem;
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

	.feed-btn.special {
		color: var(--text-muted);
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
