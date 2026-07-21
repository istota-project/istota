<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { ChevronLeft, ChevronRight, Star, X, ExternalLink } from 'lucide-svelte';
  import type { FeedEntry } from '$lib/api';
  import { updateEntryStarred } from '$lib/api';

  let {
    entries = [],
    index = null,
    hasMore = false,
    onClose,
    onView,
    onStarToggle,
    onImageClick,
    onNeedMore,
  }: {
    entries?: FeedEntry[];
    index?: number | null;
    /** Whether the current view has more entries to page in (server-side). */
    hasMore?: boolean;
    onClose: () => void;
    onView?: (id: number) => void;
    onStarToggle?: (id: number, starred: boolean) => void;
    onImageClick?: (images: string[], idx: number) => void;
    /** Ask the page to load the next batch of the current view. Resolves
     *  once entries have grown (or there's nothing more). */
    onNeedMore?: () => Promise<void>;
  } = $props();

  let current = $state<number | null>(null);
  let bodyEl = $state<HTMLElement | null>(null);
  let loadingMore = $state(false);

  $effect(() => {
    current = index;
  });

  const entry = $derived(
    current !== null && current >= 0 && current < entries.length ? entries[current] : null,
  );
  const hasPrev = $derived(current !== null && current > 0);
  // Next exists if there's another loaded entry, or the current view can page
  // in more (arrows span the whole view, not just the loaded slice).
  const hasNext = $derived(
    current !== null && (current < entries.length - 1 || (hasMore && !!onNeedMore)),
  );
  const permalink = $derived(entry ? entry.url || entry.feed.site_url || '' : '');

  // Mark read + scroll to top whenever we land on an entry.
  $effect(() => {
    if (entry) {
      onView?.(entry.id);
      if (bodyEl) bodyEl.scrollTop = 0;
    }
  });

  function prev(e?: Event) {
    e?.stopPropagation();
    if (hasPrev && current !== null) current = current - 1;
  }

  async function next(e?: Event) {
    e?.stopPropagation();
    if (current === null || loadingMore) return;
    if (current < entries.length - 1) {
      current = current + 1;
      return;
    }
    // At the loaded boundary: pull the next page of the current view, then
    // advance if it grew. Respects the active filter (feed/category/unread/
    // starred) because the page loads with those same params.
    if (hasMore && onNeedMore) {
      loadingMore = true;
      try {
        const before = entries.length;
        await onNeedMore();
        await tick();
        if (entries.length > before) current = current + 1;
      } finally {
        loadingMore = false;
      }
    }
  }

  async function toggleStar(e: MouseEvent) {
    e.stopPropagation();
    if (!entry) return;
    const target = entry;
    const nextStarred = !target.starred;
    target.starred = nextStarred;
    try {
      await updateEntryStarred(target.id, nextStarred);
      onStarToggle?.(target.id, nextStarred);
    } catch {
      target.starred = !nextStarred;
    }
  }

  function formatDate(iso: string): string {
    try {
      return new Date(iso).toLocaleDateString('en-US', {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
      });
    } catch {
      return '';
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    if (current === null) return;
    if (e.key === 'Escape') onClose();
    else if (e.key === 'ArrowRight') next();
    else if (e.key === 'ArrowLeft') prev();
  }

  onMount(() => {
    document.addEventListener('keydown', handleKeydown);
    return () => document.removeEventListener('keydown', handleKeydown);
  });

  // Lock background scroll while the reader is open.
  $effect(() => {
    if (typeof document === 'undefined') return;
    const open = entry !== null;
    document.body.style.overflow = open ? 'hidden' : '';
    return () => {
      document.body.style.overflow = '';
    };
  });
</script>

{#if entry}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="reader-backdrop" onclick={onClose}>
    <button class="nav prev" onclick={prev} disabled={!hasPrev} aria-label="Previous post">
      <ChevronLeft size={28} />
    </button>
    <button
      class="nav next"
      class:loading={loadingMore}
      onclick={next}
      disabled={!hasNext || loadingMore}
      aria-label="Next post"
    >
      <ChevronRight size={28} />
    </button>

    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <article class="reader-panel" onclick={(e) => e.stopPropagation()}>
      <header class="reader-head">
        <span class="feed-name">{entry.feed.title}</span>
        {#if entry.published_at}
          <span class="dot">·</span>
          <time datetime={entry.published_at}>{formatDate(entry.published_at)}</time>
        {/if}
        <span class="spacer"></span>
        <button
          type="button"
          class="icon-btn star"
          class:starred={entry.starred}
          onclick={toggleStar}
          aria-label={entry.starred ? 'Unstar' : 'Star'}
        >
          <Star size={18} fill={entry.starred ? 'currentColor' : 'none'} />
        </button>
        {#if permalink}
          <a
            class="icon-btn"
            href={permalink}
            target="_blank"
            rel="noopener"
            aria-label="Open original"
          >
            <ExternalLink size={18} />
          </a>
        {/if}
        <button type="button" class="icon-btn" onclick={onClose} aria-label="Close">
          <X size={20} />
        </button>
      </header>

      <div class="reader-body" bind:this={bodyEl}>
        {#if entry.title}
          <h1 class="reader-title">
            {#if permalink}
              <a href={permalink} target="_blank" rel="noopener">{entry.title}</a>
            {:else}{entry.title}{/if}
          </h1>
        {/if}

        {#if entry.images.length > 0}
          <div class="reader-hero" class:multi={entry.images.length > 1}>
            {#each entry.images as img, i}
              <button
                type="button"
                class="hero-img"
                onclick={() => onImageClick?.(entry.images, i)}
              >
                <img src={img} alt={entry.title || ''} loading="lazy" />
              </button>
            {/each}
          </div>
        {/if}

        {#if entry.content}
          <div class="reader-content">{@html entry.content}</div>
        {/if}

        {#if permalink}
          <a class="open-original" href={permalink} target="_blank" rel="noopener">
            Open original <ExternalLink size={15} />
          </a>
        {/if}
      </div>
    </article>
  </div>
{/if}

<style>
  /* Mirror the Lightbox backdrop so the two overlays feel like one surface.
	   align-items: center keeps the panel vertically centered; the panel caps
	   at 94vh and scrolls internally, so a short post centers on screen while a
	   long one fills the height without overflowing the viewport. */
  .reader-backdrop {
    position: fixed;
    inset: 0;
    z-index: 60;
    background: rgba(0, 0, 0, 0.9);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 3vh 1rem;
    overflow: auto;
  }

  /* Same surface/border/radius tokens the grid & list cards use. */
  .reader-panel {
    position: relative;
    width: 100%;
    max-width: 720px;
    background: var(--surface-card);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-card);
    box-shadow: 0 24px 64px rgba(0, 0, 0, 0.5);
    display: flex;
    flex-direction: column;
    max-height: 94vh;
    overflow: hidden;
  }

  .reader-head {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.6rem 0.85rem;
    border-bottom: 1px solid var(--border-subtle);
    font-size: var(--text-sm);
    color: var(--text-dim); /* matches the card .meta row */
  }

  .reader-head .feed-name {
    font-weight: 600;
    color: var(--text-muted);
  }

  .reader-head .dot {
    opacity: 0.5;
  }

  .reader-head .spacer {
    flex: 1;
  }

  .icon-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0.3rem;
    border: none;
    background: none;
    color: var(--text-dim);
    cursor: pointer;
    border-radius: var(--radius-card);
    transition:
      color var(--transition-fast),
      background var(--transition-fast);
  }

  .icon-btn:hover {
    color: var(--text-primary);
    background: var(--surface-raised);
  }

  .icon-btn.star.starred,
  .icon-btn.star:hover {
    color: #f5b300;
  }

  .reader-body {
    overflow-y: auto;
    padding: 1.1rem 1.4rem 1.6rem;
  }

  .reader-title {
    font-size: 1.5rem;
    line-height: 1.25;
    margin: 0 0 0.9rem;
    color: var(--text-primary);
  }

  .reader-title a {
    color: inherit;
    text-decoration: none;
  }

  .reader-title a:hover {
    text-decoration: underline;
  }

  .reader-hero {
    margin: 0 0 1.1rem;
    display: grid;
    gap: 0.4rem;
  }

  .reader-hero.multi {
    grid-template-columns: repeat(2, 1fr);
  }

  .hero-img {
    border: none;
    padding: 0;
    background: #0e0e0e; /* matches the grid .card-image letterbox */
    cursor: zoom-in;
    border-radius: var(--radius-card);
    overflow: hidden;
  }

  .hero-img img {
    display: block;
    width: 100%;
    height: auto;
  }

  /* Body copy mirrors the card .excerpt (--text-secondary + #aaa links). */
  .reader-content {
    color: var(--text-secondary);
    line-height: 1.6;
    font-size: var(--text-base);
    word-break: break-word;
  }

  .reader-content :global(img) {
    max-width: 100%;
    height: auto;
    border-radius: var(--radius-card);
    margin: 0.6rem 0;
  }

  .reader-content :global(a) {
    color: #aaa;
    text-decoration: underline;
  }

  .reader-content :global(a:hover) {
    color: var(--text-primary);
  }

  .reader-content :global(p) {
    margin: 0 0 0.85rem;
  }

  .reader-content :global(figure) {
    margin: 0.8rem 0;
  }

  .open-original {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    margin-top: 1.2rem;
    padding: 0.45rem 0.8rem;
    border-radius: var(--radius-card);
    background: var(--surface-raised);
    color: var(--text-primary);
    font-size: var(--text-sm);
    font-weight: 500;
    text-decoration: none;
  }

  .open-original:hover {
    background: var(--surface-badge);
  }

  /* Same treatment as the Lightbox nav buttons. */
  .nav {
    position: fixed;
    top: 50%;
    transform: translateY(-50%);
    display: flex;
    align-items: center;
    justify-content: center;
    width: 3rem;
    height: 3rem;
    border: none;
    border-radius: 50%;
    background: rgba(0, 0, 0, 0.5);
    color: #fff;
    cursor: pointer;
    z-index: 61;
    transition: background var(--transition-fast);
  }

  .nav:hover:not(:disabled) {
    background: rgba(0, 0, 0, 0.75);
  }

  .nav:disabled {
    opacity: 0.25;
    cursor: default;
  }

  .nav.loading {
    opacity: 0.6;
    cursor: progress;
  }

  .nav.prev {
    left: max(1rem, calc(50vw - 420px));
  }

  .nav.next {
    right: max(1rem, calc(50vw - 420px));
  }

  @media (max-width: 640px) {
    .nav {
      display: none;
    }
  }
</style>
