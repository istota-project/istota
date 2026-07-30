<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { ChevronLeft, ChevronRight, FileText, Play, Star, X, ExternalLink } from 'lucide-svelte';
  import type { FeedEntry } from '$lib/api';
  import { updateEntryStarred } from '$lib/api';
  import { fileKind, playerUrl, providerLabel } from '$lib/feeds/embed';
  import { notifyError } from '$lib/stores/notices';

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
  const hasImages = $derived(!!entry && entry.images.length > 0);

  // Same media reasoning as FeedCard, one click further in: an Are.na Embed
  // block's hero plays rather than zooming, and an Attachment's cover opens
  // the file rather than zooming page 1 as a picture. `playerUrl` is an
  // allowlist and returns null for anything it can't vouch for, in which case
  // this degrades to the ordinary lightbox hero and the body's "Watch on …"
  // link is the way out. Nothing is guessed into an iframe src.
  const player = $derived(entry ? playerUrl(entry.embed_url) : null);
  const providerName = $derived(entry ? providerLabel(entry.embed_url) : '');
  const playLabel = $derived(`Play video${providerName ? ` on ${providerName}` : ''}`);
  const documentUrl = $derived(!player && entry?.file_url ? entry.file_url : '');
  const documentKind = $derived(fileKind(documentUrl));

  // Keyed on the entry rather than a bare boolean, so paging to the next post
  // can't leave a previous video's frame mounted over it.
  let playingId = $state<number | null>(null);
  const playing = $derived(!!entry && playingId === entry.id);
  const playerSrc = $derived(player ? `${player}?autoplay=1` : '');

  // Autoplay only ever follows an explicit click, never landing on the entry.
  function play(e: MouseEvent) {
    e.stopPropagation();
    e.preventDefault();
    if (entry) playingId = entry.id;
  }

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
      notifyError("Couldn't update star.");
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
  <div class="reader-backdrop overlay-safe" onclick={onClose}>
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

        {#if player}
          <div class="reader-hero">
            {#if playing}
              <div class="reader-player">
                <iframe
                  src={playerSrc}
                  title={entry.title || 'Embedded video'}
                  allow="autoplay; fullscreen; picture-in-picture; encrypted-media"
                  sandbox="allow-scripts allow-same-origin allow-presentation allow-popups allow-popups-to-escape-sandbox"
                  referrerpolicy="strict-origin-when-cross-origin"
                  loading="lazy"
                  allowfullscreen
                ></iframe>
              </div>
            {:else}
              <button
                type="button"
                class="reader-video"
                class:no-poster={!hasImages}
                onclick={play}
                aria-label={playLabel}
              >
                {#if hasImages}
                  <img src={entry.images[0]} alt={entry.title || ''} loading="lazy" />
                {/if}
                <span class="play-badge"><Play size={32} fill="currentColor" /></span>
              </button>
            {/if}
          </div>
        {:else if documentUrl}
          <div class="reader-hero">
            <!-- A real <a>, so middle-click and copy-link keep working. -->
            <a
              class="reader-document"
              class:no-cover={!hasImages}
              href={documentUrl}
              target="_blank"
              rel="noopener noreferrer"
              aria-label={`Open ${documentKind}: ${entry.title || 'attached document'}`}
            >
              {#if hasImages}
                <img src={entry.images[0]} alt={entry.title || ''} loading="lazy" />
              {:else}
                <FileText size={40} aria-hidden="true" />
              {/if}
              <span class="doc-badge">{documentKind}</span>
            </a>
          </div>
        {:else if entry.images.length > 0}
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

        {#if (entry.duplicate_image_count ?? 0) > 0}
          <p class="repeat-note">
            {entry.duplicate_image_count} image{entry.duplicate_image_count > 1 ? 's' : ''} hidden — already
            shown by a more recent post.
          </p>
        {/if}

        {#if entry.content}
          <div class="reader-content prose">{@html entry.content}</div>
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
	   at the padded box and scrolls internally, so a short post centers on
	   screen while a long one fills the height without overflowing the viewport.

	   Padding comes from .overlay-safe (app.css) — the scrim stays edge to edge
	   while the panel keeps clear of the Dynamic Island and the home indicator.
	   These two values are the no-inset baseline it raises. */
  .reader-backdrop {
    position: fixed;
    inset: 0;
    z-index: var(--z-viewer);
    /* design-lint-allow: fixed chrome — a modal scrim is dark in both themes;
       it exists to darken whatever is behind it, not to follow the surface. */
    background: rgba(0, 0, 0, 0.9);
    display: flex;
    align-items: center;
    justify-content: center;
    --overlay-pad-block: 3vh;
    --overlay-pad-inline: 1rem;
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
    box-shadow: var(--shadow-lg);
    display: flex;
    flex-direction: column;
    /* Resolves against the backdrop's padded content box, so the cap tracks the
		   safe-area insets instead of restating a vh figure that ignores them. */
    max-height: 100%;
    overflow: hidden;
  }

  .reader-head {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 0.75rem;
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
    color: var(--accent-amber);
  }

  /* Same 0.5rem/0.75rem the grid card's .card-body / .excerpt / .meta use, so
	   a post sits at the same inset inline and expanded. */
  .reader-body {
    overflow-y: auto;
    padding: 0.5rem 0.75rem;
  }

  .reader-title {
    font-size: 1rem;
    line-height: 1.25;
    margin: 0 0 1rem;
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
    /* design-lint-allow: fixed chrome — letterbox behind media of unknown
       aspect ratio; stays dark in both themes so the image reads as the
       lit surface. */
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

  /* Media heroes. Same visual language as the grid's .card-video /
	   .card-document (see routes/feeds/+page.svelte) — the difference is the
	   corner radius, which is rounded on all four here because the reader's
	   hero floats inside the panel rather than capping a card. */
  .reader-video,
  .reader-document {
    position: relative;
    display: grid;
    justify-items: center;
    border: none;
    padding: 0;
    /* design-lint-allow: fixed chrome — letterbox behind media of unknown
       aspect ratio; stays dark in both themes so the image reads as the
       lit surface. */
    background: #0e0e0e;
    border-radius: var(--radius-card);
    overflow: hidden;
    cursor: pointer;
    text-decoration: none;
  }

  .reader-video img,
  .reader-document img {
    display: block;
    width: 100%;
    height: auto;
  }

  /* A block that carried no thumbnail / cover page still needs a target;
	   without one it would collapse to zero height. */
  .reader-video.no-poster,
  .reader-document.no-cover {
    min-height: 180px;
    align-content: center;
    color: var(--text-muted);
  }

  .reader-video .play-badge {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    display: flex;
    align-items: center;
    justify-content: center;
    width: 64px;
    height: 64px;
    border-radius: 50%;
    /* design-lint-allow-begin: fixed chrome — a dark scrim over media, so both
       the scrim and the glyph on it are fixed in both themes. */
    background: rgb(0 0 0 / 0.6);
    color: #fff;
    /* design-lint-allow-end */
    /* A triangle's optical centre sits left of its bounding box's. */
    padding-left: 5px;
    transition: background 0.15s ease;
    pointer-events: none;
  }

  .reader-video:hover .play-badge {
    /* design-lint-allow: fixed chrome — the scrim above, one step darker. */
    background: rgb(0 0 0 / 0.8);
  }

  .reader-document .doc-badge {
    position: absolute;
    top: 0.5rem;
    right: 0.5rem;
    padding: 0.15rem 0.4rem;
    border-radius: var(--radius-sm);
    /* design-lint-allow-begin: fixed chrome — a dark scrim over media, so both
       the scrim and the glyph on it are fixed in both themes. */
    background: rgb(0 0 0 / 0.7);
    color: #fff;
    /* design-lint-allow-end */
    font-size: 0.65rem;
    font-weight: 600;
    letter-spacing: 0.04em;
  }

  .reader-player {
    width: 100%;
    aspect-ratio: 16 / 9;
    /* design-lint-allow: fixed chrome — letterbox behind media of unknown
       aspect ratio; stays dark in both themes so the image reads as the
       lit surface. */
    background: #000;
    border-radius: var(--radius-card);
    overflow: hidden;
  }

  .reader-player iframe {
    width: 100%;
    height: 100%;
    border: 0;
    display: block;
  }

  /* Body copy mirrors the card .excerpt. Link color comes from the shared
	   `prose` contract in app.css, not from here. */
  .repeat-note {
    margin: 0 0 1rem;
    font-size: var(--text-sm);
    color: var(--text-dim);
    font-style: italic;
  }

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
    /* design-lint-allow-begin: fixed chrome — a dark scrim over media, so both
       the scrim and the glyph on it are fixed in both themes. */
    background: rgba(0, 0, 0, 0.5);
    color: #fff;
    /* design-lint-allow-end */
    cursor: pointer;
    z-index: var(--z-viewer-control);
    transition: background var(--transition-fast);
  }

  .nav:hover:not(:disabled) {
    /* design-lint-allow: fixed chrome — the scrim above, one step darker. */
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
