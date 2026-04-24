<script lang="ts">
	import type { FeedEntry } from '$lib/api';

	import { markReadDelay } from '$lib/stores/feeds';

	let { entry, onImageClick, onViewed }: {
		entry: FeedEntry;
		onImageClick: (url: string) => void;
		onViewed?: (id: number) => void;
	} = $props();

	const maxGrid = 4;
	const feedSlug = $derived(entry.feed.title.toLowerCase().replace(/[^a-z0-9-]/g, '-'));
	const isImage = $derived(entry.images.length > 0);
	const hiddenCount = $derived(Math.max(0, entry.images.length - maxGrid));
	const permalink = $derived(entry.url || entry.feed.site_url || '');

	function formatDate(iso: string): string {
		try {
			const d = new Date(iso);
			return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
		} catch {
			return '';
		}
	}

	function trackView(node: HTMLElement) {
		if (entry.status === 'read' || !onViewed) return;

		let timer: ReturnType<typeof setTimeout> | null = null;
		let done = false;

		const observer = new IntersectionObserver(
			(entries) => {
				const e = entries[0];
				if (e.isIntersecting && !done) {
					timer = setTimeout(() => {
						done = true;
						onViewed!(entry.id);
						observer.disconnect();
					}, $markReadDelay * 1000);
				} else if (timer) {
					clearTimeout(timer);
					timer = null;
				}
			},
			{ threshold: 0.5 },
		);

		observer.observe(node);

		return {
			destroy() {
				if (timer) clearTimeout(timer);
				observer.disconnect();
			},
		};
	}
</script>

<article
	class="card {isImage ? 'image' : 'text'} feed-{feedSlug}"
	data-published={entry.published_at}
	data-added={entry.created_at}
	use:trackView
>
	{#if entry.status === 'read'}
		<span class="seen-pill">SEEN</span>
	{/if}
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
				{#if permalink}<a href={permalink}>{entry.title}</a>{:else}{entry.title}{/if}
			</div>
		{/if}
		{#if entry.content}
			<div class="card-body"><div class="excerpt">{@html entry.content}</div></div>
		{/if}
	{:else}
		<div class="card-body">
			{#if entry.title}
				<h3>{#if permalink}<a href={permalink}>{entry.title}</a>{:else}{entry.title}{/if}</h3>
			{/if}
			{#if entry.content}
				<div class="excerpt">{@html entry.content}</div>
			{/if}
		</div>
	{/if}
	<div class="meta">
		<span class="feed-name">{entry.feed.title}</span>
		{#if entry.published_at}
			{#if permalink}
				<a href={permalink} class="meta-link">
					<time datetime={entry.published_at}>{formatDate(entry.published_at)}</time>
				</a>
			{:else}
				<span class="meta-link">
					<time datetime={entry.published_at}>{formatDate(entry.published_at)}</time>
				</span>
			{/if}
		{/if}
	</div>
</article>
