<script lang="ts">
  import { Star } from 'lucide-svelte';
  import type { FeedEntry } from '$lib/api';
  import { updateEntryStarred } from '$lib/api';

  import { markReadDelay } from '$lib/stores/feeds';

  let {
    entry,
    onImageClick,
    onViewed,
    onStarToggle,
    onOpen,
  }: {
    entry: FeedEntry;
    onImageClick: (images: string[], index: number) => void;
    onViewed?: (id: number) => void;
    onStarToggle?: (id: number, starred: boolean) => void;
    onOpen?: () => void;
  } = $props();

  // Open the reader on a plain card click, but let the existing interactive
  // targets (image → lightbox, title/permalink → original, star) keep their
  // own behaviour.
  function handleCardClick(e: MouseEvent) {
    if (!onOpen) return;
    if ((e.target as HTMLElement).closest('a, button')) return;
    onOpen();
  }

  function handleCardKey(e: KeyboardEvent) {
    if (!onOpen || e.target !== e.currentTarget) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onOpen();
    }
  }

  async function toggleStar(e: MouseEvent) {
    e.stopPropagation();
    e.preventDefault();
    const next = !entry.starred;
    // Optimistic local update; the parent owns the entries array, so we
    // poke a callback so it can rebroadcast (e.g. exit a starred-only view).
    entry.starred = next;
    try {
      await updateEntryStarred(entry.id, next);
      onStarToggle?.(entry.id, next);
    } catch {
      // Roll back if the server rejected.
      entry.starred = !next;
    }
  }

  const maxGrid = 4;
  const feedSlug = $derived(entry.feed.title.toLowerCase().replace(/[^a-z0-9-]/g, '-'));
  const isImage = $derived(entry.images.length > 0);
  const hiddenCount = $derived(Math.max(0, entry.images.length - maxGrid));
  // Images the server withheld because a newer entry already showed them
  // (reblog of a picture you just scrolled past). Noted rather than silent,
  // so a card that lost all its images doesn't just look empty.
  const repeatCount = $derived(entry.duplicate_image_count ?? 0);
  const galleryCount = $derived(Math.min(entry.images.length, maxGrid));
  const permalink = $derived(entry.url || entry.feed.site_url || '');

  // The Images / Text header chips are *display* toggles, not filters, and this
  // card deliberately knows nothing about them: it always renders its media and
  // body, and the grid hides them with CSS (the .hide-images / .hide-text rules
  // in routes/feeds/+page.svelte). That keeps the toggles desktop-only for free
  // — the rules live in a min-width media query, so on a phone they simply
  // don't apply and everything shows. Conditioning the markup here instead
  // would need JS viewport detection, which nothing else in this app does, and
  // would flash the wrong layout between prerender and hydration. It is also
  // the only way to reach images embedded in the body copy, which arrive as
  // {@html} and can't be conditioned in the template at all.

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

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<article
  class="card {isImage ? 'image' : 'text'} feed-{feedSlug}"
  class:openable={!!onOpen}
  data-published={entry.published_at}
  data-added={entry.created_at}
  use:trackView
  onclick={handleCardClick}
  onkeydown={handleCardKey}
  role={onOpen ? 'button' : undefined}
  tabindex={onOpen ? 0 : undefined}
>
  {#if entry.status === 'read'}
    <span class="seen-pill">SEEN</span>
  {/if}
  {#if isImage}
    {#if entry.images.length > 1}
      <div class="card-gallery gallery-{galleryCount}">
        {#each entry.images.slice(0, maxGrid) as img, idx}
          <button
            type="button"
            class="card-image{idx === maxGrid - 1 && hiddenCount > 0 ? ' gallery-more' : ''}"
            onclick={() => onImageClick(entry.images, idx)}
          >
            <img src={img} alt={entry.title || ''} loading="lazy" />
            {#if idx === maxGrid - 1 && hiddenCount > 0}
              <span class="gallery-count">+{hiddenCount + 1}</span>
            {/if}
          </button>
        {/each}
      </div>
    {:else}
      <button type="button" class="card-image" onclick={() => onImageClick(entry.images, 0)}>
        <img src={entry.images[0]} alt={entry.title || ''} loading="lazy" />
      </button>
    {/if}
    {#if entry.title}
      <div class="card-title-overlay">
        {#if permalink}<a href={permalink}>{entry.title}</a>{:else}{entry.title}{/if}
      </div>
    {/if}
    {#if entry.content}
      <div class="card-body"><div class="excerpt prose">{@html entry.content}</div></div>
    {/if}
  {:else}
    <div class="card-body">
      {#if entry.title}
        <h3>
          {#if permalink}<a href={permalink}>{entry.title}</a>{:else}{entry.title}{/if}
        </h3>
      {/if}
      {#if entry.content}
        <div class="excerpt prose">{@html entry.content}</div>
      {/if}
    </div>
  {/if}
  <div class="meta">
    <button
      type="button"
      class="star-btn"
      class:starred={entry.starred}
      onclick={toggleStar}
      title={entry.starred ? 'Unstar' : 'Star'}
      aria-label={entry.starred ? 'Unstar entry' : 'Star entry'}
    >
      <Star size={14} fill={entry.starred ? 'currentColor' : 'none'} />
    </button>
    <span class="feed-name">{entry.feed.title}</span>
    {#if repeatCount > 0}
      <span class="repeat-note" title="Already shown by a more recent post">
        {repeatCount} repeat{repeatCount > 1 ? 's' : ''} hidden
      </span>
    {/if}
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
