<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { renderMarkdown } from '$lib/markdown';
	import {
		getBriefingArchive,
		type BriefingArchiveItem,
		type BriefingArchiveResponse
	} from '$lib/api';

	let loading = $state(true);
	let error = $state<string | null>(null);
	let items = $state<BriefingArchiveItem[]>([]);
	let total = $state(0);
	let names = $state<string[]>([]);
	let filterName = $state<string>('');
	let selectedId = $state<number | null>(null);
	let offset = $state(0);
	const PAGE = 20;

	const selected = $derived(items.find((i) => i.id === selectedId) ?? items[0] ?? null);

	async function load(reset = true) {
		loading = reset;
		error = null;
		try {
			const params: Record<string, string> = { limit: String(PAGE), offset: String(offset) };
			if (filterName) params.briefing_name = filterName;
			const resp: BriefingArchiveResponse = await getBriefingArchive(params);
			items = reset ? resp.items : [...items, ...resp.items];
			total = resp.total;
			names = resp.briefing_names;
			if (reset && items.length > 0) selectedId = items[0].id;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load briefings';
		} finally {
			loading = false;
		}
	}

	function pickName(name: string) {
		filterName = name;
		offset = 0;
		selectedId = null;
		load();
	}

	function loadMore() {
		offset += PAGE;
		load(false);
	}

	function fmtDate(iso: string): string {
		try {
			return new Date(iso).toLocaleString(undefined, {
				dateStyle: 'medium',
				timeStyle: 'short'
			});
		} catch {
			return iso;
		}
	}

	onMount(() => load());
</script>

<svelte:head>
	<title>Briefings</title>
</svelte:head>

<div class="briefings">
	<aside class="sidebar">
		<h2>Briefings</h2>
		{#if names.length > 1}
			<div class="name-filter">
				<button class="chip" class:active={filterName === ''} onclick={() => pickName('')}>All</button>
				{#each names as n (n)}
					<button class="chip" class:active={filterName === n} onclick={() => pickName(n)}>{n}</button>
				{/each}
			</div>
		{/if}
		<ul class="archive-list">
			{#each items as item (item.id)}
				<li>
					<button
						class="archive-item"
						class:active={item.id === selected?.id}
						onclick={() => (selectedId = item.id)}
					>
						<span class="ai-subject">{item.subject || item.briefing_name}</span>
						<span class="ai-date">{fmtDate(item.generated_at)}</span>
					</button>
				</li>
			{/each}
		</ul>
		{#if items.length < total}
			<button class="load-more" onclick={loadMore}>Load older</button>
		{/if}
	</aside>

	<main class="reader">
		{#if loading}
			<p class="status">Loading…</p>
		{:else if error}
			<p class="status error">{error}</p>
		{:else if !selected}
			<div class="empty">
				<h1>No briefings yet</h1>
				<p class="muted">
					Once a scheduled briefing runs it will appear here. Configure blocks and
					schedule in <a href="{base}/briefings/settings">settings</a>.
				</p>
			</div>
		{:else}
			<article class="briefing">
				<header>
					<h1>{selected.subject || selected.briefing_name}</h1>
					<p class="meta">{fmtDate(selected.generated_at)}</p>
				</header>
				<!-- eslint-disable-next-line svelte/no-at-html-tags -->
				<div class="body">{@html renderMarkdown(selected.body_md ?? '')}</div>
			</article>
		{/if}
	</main>
</div>

<style>
	.briefings {
		display: flex;
		height: 100%;
		gap: 0;
	}
	.sidebar {
		width: 18rem;
		flex-shrink: 0;
		border-right: 1px solid var(--border, #2a2a2a);
		padding: 1rem;
		overflow-y: auto;
	}
	.sidebar h2 {
		margin: 0 0 0.75rem;
		font-size: 1rem;
	}
	.name-filter {
		display: flex;
		flex-wrap: wrap;
		gap: 0.25rem;
		margin-bottom: 0.75rem;
	}
	.chip {
		border: 1px solid var(--border, #333);
		background: transparent;
		color: inherit;
		border-radius: 999px;
		padding: 0.15rem 0.6rem;
		font-size: 0.8rem;
		cursor: pointer;
	}
	.chip.active {
		background: var(--accent, #3b82f6);
		border-color: var(--accent, #3b82f6);
		color: #fff;
	}
	.archive-list {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.archive-item {
		display: flex;
		flex-direction: column;
		width: 100%;
		text-align: left;
		gap: 0.15rem;
		padding: 0.5rem 0.6rem;
		border: none;
		border-radius: 0.4rem;
		background: transparent;
		color: inherit;
		cursor: pointer;
	}
	.archive-item:hover {
		background: var(--surface-hover, rgba(127, 127, 127, 0.12));
	}
	.archive-item.active {
		background: var(--surface-active, rgba(59, 130, 246, 0.15));
	}
	.ai-subject {
		font-weight: 600;
		font-size: 0.9rem;
	}
	.ai-date {
		font-size: 0.75rem;
		color: var(--text-muted, #888);
	}
	.load-more {
		margin-top: 0.5rem;
		width: 100%;
		padding: 0.4rem;
		background: transparent;
		border: 1px solid var(--border, #333);
		border-radius: 0.4rem;
		color: inherit;
		cursor: pointer;
	}
	.reader {
		flex: 1;
		overflow-y: auto;
		padding: 1.5rem 2rem;
	}
	.briefing header h1 {
		margin: 0 0 0.25rem;
	}
	.meta {
		color: var(--text-muted, #888);
		margin: 0 0 1.25rem;
		font-size: 0.85rem;
	}
	.body :global(h1),
	.body :global(h2) {
		font-size: 1.05rem;
	}
	.status {
		color: var(--text-muted, #888);
	}
	.status.error {
		color: var(--danger, #ef4444);
	}
	.empty {
		max-width: 32rem;
	}
	.muted {
		color: var(--text-muted, #888);
	}
</style>
