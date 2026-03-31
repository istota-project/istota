<script lang="ts">
	import type { FeedEntry } from '$lib/api';

	let { entry, onImageClick }: { entry: FeedEntry; onImageClick: (url: string) => void } = $props();

	const feedSlug = entry.feed.title.toLowerCase().replace(/[^a-z0-9-]/g, '-');
	const isImage = entry.images.length > 0;
	const maxGrid = 4;
	const hiddenCount = Math.max(0, entry.images.length - maxGrid);

	function formatDate(iso: string): string {
		try {
			const d = new Date(iso);
			return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
		} catch {
			return '';
		}
	}
</script>

<article
	class="card {isImage ? 'image' : 'text'} feed-{feedSlug}"
	data-published={entry.published_at}
	data-added={entry.created_at}
>
	{#if isImage}
		{#if entry.images.length > 1}
			<div class="card-gallery">
				{#each entry.images.slice(0, maxGrid) as img, idx}
					<button
						type="button"
						class="card-image{idx === maxGrid - 1 && hiddenCount > 0 ? ' gallery-more' : ''}"
						onclick={() => onImageClick(img)}
					>
						<img src={img} alt={entry.title || ''} loading="lazy" />
						{#if idx === maxGrid - 1 && hiddenCount > 0}
							<span class="gallery-count">+{hiddenCount + 1}</span>
						{/if}
					</button>
				{/each}
			</div>
		{:else}
			<button type="button" class="card-image" onclick={() => onImageClick(entry.images[0])}>
				<img src={entry.images[0]} alt={entry.title || ''} loading="lazy" />
			</button>
		{/if}
		{#if entry.title}
			<div class="card-title-overlay">
				{#if entry.url}<a href={entry.url}>{entry.title}</a>{:else}{entry.title}{/if}
			</div>
		{/if}
		{#if entry.content}
			<div class="card-body"><div class="excerpt">{@html entry.content}</div></div>
		{/if}
	{:else}
		<div class="card-body">
			{#if entry.title}
				<h3>{#if entry.url}<a href={entry.url}>{entry.title}</a>{:else}{entry.title}{/if}</h3>
			{/if}
			{#if entry.content}
				<div class="excerpt">{@html entry.content}</div>
			{/if}
		</div>
	{/if}
	<div class="meta">
		<span class="feed-name">{entry.feed.title}</span>
		{#if entry.published_at}
			{#if entry.url}
				<a href={entry.url} class="meta-link">
					<time datetime={entry.published_at}>{formatDate(entry.published_at)}</time>
				</a>
			{:else}
				<time datetime={entry.published_at}>{formatDate(entry.published_at)}</time>
			{/if}
		{/if}
	</div>
</article>
