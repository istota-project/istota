<script lang="ts">
  import { onMount, onDestroy, tick } from 'svelte';
  import {
    getFeeds,
    markAsRead,
    updateEntriesStatus,
    updateEntryStarred,
    type FeedEntry,
  } from '$lib/api';
  import {
    feedsRefreshNonce,
    selectedFeedId,
    selectedCategoryId,
    showImages,
    showStarred,
    showText,
    showUnseen,
    sortBy,
    viewMode,
  } from '$lib/stores/feeds';
  import FeedCard from '$lib/components/FeedCard.svelte';
  import FeedReader from '$lib/components/FeedReader.svelte';
  import Lightbox from '$lib/components/Lightbox.svelte';
  import { getShellScrollRoot } from '$lib/components/ui/AppShell.svelte';

  const getScrollRoot = getShellScrollRoot();

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

  // Reload when the user enters / leaves the Starred view.
  let prevStarred = false;
  $effect(() => {
    if ($showStarred !== prevStarred) {
      loadEntries($selectedFeedId);
    }
    prevStarred = $showStarred;
  });

  // Bumped by toolbar (mark-all) and similar bulk ops; force a reload.
  let prevNonce = 0;
  $effect(() => {
    if ($feedsRefreshNonce !== prevNonce) {
      loadEntries($selectedFeedId);
    }
    prevNonce = $feedsRefreshNonce;
  });

  // Sort toggle: server-side order needs a reload to take effect.
  let prevSort: typeof $sortBy = $sortBy;
  $effect(() => {
    if ($sortBy !== prevSort) {
      loadEntries($selectedFeedId);
    }
    prevSort = $sortBy;
  });

  // View-mode toggle: keep the topmost visible card pinned across grid/list
  // switches. Capture before the class flips (effect.pre), restore after the
  // next render tick.
  let prevView: typeof $viewMode = $viewMode;
  $effect.pre(() => {
    const next = $viewMode;
    if (next === prevView) return;
    prevView = next;

    const root = getScrollRoot?.();
    if (!root) return;

    const rootTop = root.getBoundingClientRect().top;
    const slots = root.querySelectorAll<HTMLElement>('.card-slot[data-entry-id]');
    let anchorId: string | null = null;
    for (const slot of slots) {
      const card = slot.firstElementChild as HTMLElement | null;
      if (!card) continue;
      // First card whose bottom is past the viewport top is the topmost
      // visible one.
      if (card.getBoundingClientRect().bottom > rootTop + 1) {
        anchorId = slot.dataset.entryId ?? null;
        break;
      }
    }
    if (!anchorId) return;

    tick().then(() => {
      const slot = root.querySelector<HTMLElement>(`.card-slot[data-entry-id="${anchorId}"]`);
      const card = slot?.firstElementChild as HTMLElement | null;
      if (!card) return;
      const rootRect = root.getBoundingClientRect();
      const cardRect = card.getBoundingClientRect();
      root.scrollTop += cardRect.top - rootRect.top;
    });
  });

  // Lightbox
  let lightboxImages = $state<string[]>([]);
  let lightboxIndex = $state<number | null>(null);

  // Reader overlay — index into entries of the open post (null = closed).
  let readerIndex = $state<number | null>(null);

  // Batch read queue
  const pendingReadIds = new Set<number>();
  let flushTimer: ReturnType<typeof setTimeout> | null = null;
  let flushMaxTimer: ReturnType<typeof setTimeout> | null = null;

  function flushPending() {
    if (pendingReadIds.size === 0) return;
    const ids = [...pendingReadIds];
    pendingReadIds.clear();
    if (flushTimer) {
      clearTimeout(flushTimer);
      flushTimer = null;
    }
    if (flushMaxTimer) {
      clearTimeout(flushMaxTimer);
      flushMaxTimer = null;
    }
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
      order: $sortBy === 'added' ? 'created_at' : 'published_at',
      direction: 'desc',
    };
    if (opts.feedId) params.feed_id = String(opts.feedId);
    if ($selectedCategoryId) params.category_id = String($selectedCategoryId);
    if ($showUnseen) params.status = 'unread';
    if ($showStarred) params.starred = '1';
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
    getScrollRoot?.()?.scrollTo(0, 0);
  }

  async function loadMore() {
    if (loadingMore || !hasMore) return;
    loadingMore = true;
    try {
      // Under the unread filter, offset is unstable: cards get marked read
      // mid-scroll, shrinking the server's unread pool and shifting offsets.
      // Use a `before` cursor on the active sort column instead.
      let opts: PageOpts;
      if ($showUnseen && entries.length > 0) {
        const oldest = entries[entries.length - 1];
        const cursorIso = $sortBy === 'added' ? oldest.created_at : oldest.published_at;
        const oldestTs = cursorIso ? Math.floor(new Date(cursorIso).getTime() / 1000) : 0;
        if (!oldestTs) {
          hasMore = false;
          return;
        }
        // +1 to include entries at exactly oldestTs (`before` is strictly
        // less-than); dedup drops any already-loaded overlap.
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
          // Under cursor mode the API returns total-matching-filter,
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
    // Re-observe sentinel so the observer fires again if it's still visible —
    // a short page (or one whose cards are compact because the Images chip is
    // off) can leave it in view across the whole load.
    if (hasMore && sentinel && scrollObserver) {
      scrollObserver.unobserve(sentinel);
      scrollObserver.observe(sentinel);
    }
  }

  // Infinite scroll sentinel
  let sentinel: HTMLDivElement | undefined = $state();
  let scrollObserver: IntersectionObserver | null = null;

  // Reload when the selected scope (feed or category) changes. Tracked
  // together so switching straight from a feed to a category — which clears
  // one store and sets the other in the same tick — reloads once, not twice.
  let prevScope: { feed: number; cat: number } | null = null;
  $effect(() => {
    const feed = $selectedFeedId;
    const cat = $selectedCategoryId;
    if (prevScope !== null && (feed !== prevScope.feed || cat !== prevScope.cat)) {
      loadEntries(feed);
    }
    prevScope = { feed, cat };
  });

  onMount(() => loadEntries($selectedFeedId));

  $effect(() => {
    if (!sentinel) return;
    scrollObserver?.disconnect();
    scrollObserver = new IntersectionObserver(
      (observed) => {
        if (observed[0].isIntersecting) loadMore();
      },
      { root: getScrollRoot?.() ?? null, rootMargin: '600px' },
    );
    scrollObserver.observe(sentinel);
    return () => scrollObserver?.disconnect();
  });

  onDestroy(() => {
    if (flushTimer) clearTimeout(flushTimer);
    if (flushMaxTimer) clearTimeout(flushMaxTimer);
    flushPending();
  });

  $effect(() => {
    window.addEventListener('keydown', handleKeydown);
    return () => window.removeEventListener('keydown', handleKeydown);
  });

  function handleStarToggle(id: number, starred: boolean) {
    const idx = entries.findIndex((e) => e.id === id);
    if (idx >= 0) {
      entries[idx] = { ...entries[idx], starred };
      // In starred-only view, drop the entry once it's unstarred so it
      // disappears immediately like the unread filter does.
      if ($showStarred && !starred) {
        entries = entries.filter((e) => e.id !== id);
        total = Math.max(0, total - 1);
      }
    }
  }

  // Keyboard shortcut: A marks every visible entry read (scope-aware).
  // f toggles star on the focused entry — left to Tranche B's full
  // shortcut layer; for now A is the highest-leverage one.
  function isEditableTarget(t: EventTarget | null): boolean {
    const el = t as HTMLElement | null;
    if (!el) return false;
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') return true;
    if ((el as HTMLElement).isContentEditable) return true;
    return false;
  }

  async function handleKeydown(e: KeyboardEvent) {
    if (isEditableTarget(e.target)) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === 'A' && e.shiftKey) {
      e.preventDefault();
      if (entries.length === 0) return;
      const maxId = Math.max(...entries.map((x) => x.id));
      const scope = $selectedFeedId ? 'feed' : 'all';
      const opts = $selectedFeedId
        ? { id: $selectedFeedId, before_id: maxId }
        : { before_id: maxId };
      try {
        await markAsRead(scope, opts);
        feedsRefreshNonce.update((n) => n + 1);
      } catch {
        // non-fatal
      }
      return;
    }
    if (e.key === 'f' && !e.shiftKey && focusedEntryId != null) {
      e.preventDefault();
      const target = entries.find((x) => x.id === focusedEntryId);
      if (!target) return;
      const next = !target.starred;
      handleStarToggle(target.id, next);
      try {
        await updateEntryStarred(target.id, next);
      } catch {
        handleStarToggle(target.id, !next);
      }
    }
  }

  // Track which card the cursor most recently entered so 'f' has a target.
  let focusedEntryId: number | null = $state(null);
</script>

<div class="feed-page">
  {#if loading}
    <div class="center-msg">Loading feeds...</div>
  {:else if error}
    <div class="center-msg error">{error}</div>
  {:else}
    <div
      class="feed-grid"
      class:list-view={$viewMode === 'list'}
      class:hide-images={!$showImages}
      class:hide-text={!$showText}
    >
      {#each entries as entry, i (entry.id)}
        <div
          class="card-slot"
          data-entry-id={entry.id}
          onmouseenter={() => (focusedEntryId = entry.id)}
          onfocusin={() => (focusedEntryId = entry.id)}
          role="presentation"
        >
          <FeedCard
            {entry}
            onImageClick={(imgs, idx) => {
              lightboxImages = imgs;
              lightboxIndex = idx;
            }}
            onViewed={handleViewed}
            onStarToggle={handleStarToggle}
            onOpen={() => (readerIndex = i)}
          />
        </div>
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

<FeedReader
  {entries}
  index={readerIndex}
  {hasMore}
  onClose={() => (readerIndex = null)}
  onView={handleViewed}
  onStarToggle={handleStarToggle}
  onNeedMore={loadMore}
  onImageClick={(imgs, idx) => {
    lightboxImages = imgs;
    lightboxIndex = idx;
  }}
/>

<Lightbox images={lightboxImages} index={lightboxIndex} onClose={() => (lightboxIndex = null)} />

<style>
  .center-msg {
    min-height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-dim);
    font-size: var(--text-sm);
  }

  .center-msg.error {
    color: var(--status-danger-fg);
  }

  .feed-page {
    min-height: 100%;
    padding: 0.75rem;
  }

  /* Grid layout */
  .feed-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(320px, 100%), 1fr));
    gap: 1rem;
  }

  .feed-grid.list-view {
    grid-template-columns: 1fr;
    max-width: 640px;
    margin: 0 auto;
  }

  .feed-grid .card-slot {
    display: contents;
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

  .feed-grid :global(.card.openable) {
    cursor: pointer;
    transition:
      box-shadow var(--transition-fast),
      transform var(--transition-fast);
  }

  .feed-grid :global(.card.openable:hover) {
    box-shadow: 0 0 0 1px var(--border-default);
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

  .feed-grid :global(.star-btn) {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    background: none;
    color: var(--text-dim);
    border: none;
    cursor: pointer;
    transition: color var(--transition-fast);
  }

  .feed-grid :global(.star-btn.starred),
  .feed-grid :global(.star-btn:hover) {
    color: var(--accent-amber);
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

  /* Playable media (Are.na Embed blocks). The poster is a play surface, not
	   a lightbox trigger, so it overrides .card-image's zoom-in cursor. */
  .feed-grid :global(.card-video) {
    position: relative;
    cursor: pointer;
  }

  /* An embed whose block carried no thumbnail still gets a play target;
	   without a poster it would otherwise collapse to zero height. */
  .feed-grid :global(.card-video.no-poster) {
    min-height: 160px;
    align-items: center;
    border-radius: var(--radius-card) var(--radius-card) 0 0;
  }

  .feed-grid :global(.card-video .play-badge) {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    display: flex;
    align-items: center;
    justify-content: center;
    width: 56px;
    height: 56px;
    border-radius: 50%;
    background: rgb(0 0 0 / 0.6);
    color: #fff;
    /* Nudge the glyph off centre — a triangle's optical centre sits left
		   of its bounding box's. */
    padding-left: 4px;
    transition: background 0.15s ease;
    pointer-events: none;
  }

  .feed-grid :global(.card-video:hover .play-badge) {
    background: rgb(0 0 0 / 0.8);
  }

  .feed-grid :global(.card-player) {
    width: 100%;
    aspect-ratio: 16 / 9;
    background: #000;
    border-radius: var(--radius-card) var(--radius-card) 0 0;
    overflow: hidden;
  }

  .feed-grid :global(.card-player iframe) {
    width: 100%;
    height: 100%;
    border: 0;
    display: block;
  }

  /* Gallery — fixed-height grid so meta strip stays visible inside the
	   card's max-height budget. Cells use object-fit: cover instead of
	   per-image aspect-ratio to keep the gallery from dictating card height. */
  .feed-grid :global(.card-gallery) {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    grid-template-rows: repeat(2, 1fr);
    height: 320px;
    gap: 2px;
  }

  .feed-grid :global(.card-gallery.gallery-2) {
    grid-template-rows: 1fr;
  }

  .feed-grid :global(.card-gallery.gallery-3 .card-image:first-child) {
    grid-row: span 2;
  }

  .feed-grid :global(.card-gallery .card-image) {
    min-width: 0;
    min-height: 0;
    overflow: hidden;
  }

  .feed-grid :global(.card-gallery .card-image img) {
    width: 100%;
    height: 100%;
    aspect-ratio: auto;
    object-fit: cover;
    max-height: none;
    border-radius: 0;
  }

  .feed-grid :global(.card-gallery .card-image:first-child img) {
    border-radius: var(--radius-card) 0 0 0;
  }

  .feed-grid :global(.card-gallery .card-image:nth-child(2) img) {
    border-radius: 0 var(--radius-card) 0 0;
  }

  /* Gallery overflow */
  .feed-grid :global(.gallery-more) {
    position: relative;
  }

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

  .feed-grid :global(.card-title-overlay a) {
    color: var(--text-muted);
    text-decoration: none;
  }
  .feed-grid :global(.card-title-overlay a:hover) {
    color: var(--text-secondary);
  }

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

  .feed-grid :global(.card-body h3 a) {
    color: var(--text-primary);
    text-decoration: none;
  }
  .feed-grid :global(.card-body h3 a:hover) {
    text-decoration: underline;
  }

  /* Excerpt — body copy is kept in lockstep with the reader popup's
	   .reader-content (line-height, paragraph rhythm, image/figure spacing) so a
	   post reads the same inline and expanded. The card's 0.5rem/0.75rem inset
	   is the shared one; FeedReader's .reader-body matches it. */
  .feed-grid :global(.excerpt) {
    margin: 0;
    padding: 0.5rem 0.75rem;
    font-size: var(--text-base);
    line-height: 1.6;
    color: var(--text-secondary);
    word-break: break-word;
  }

  /* Link color comes from the shared `prose` contract in app.css. */
  .feed-grid :global(.excerpt p) {
    margin: 0 0 0.85rem;
  }

  .feed-grid :global(.excerpt figure) {
    margin: 0.8rem 0;
  }

  .feed-grid :global(.excerpt img) {
    max-width: 100%;
    height: auto;
    border-radius: var(--radius-card);
    margin: 0.6rem 0;
    display: block;
  }

  /* Images / Text display toggles — DESKTOP ONLY.

	   The header chips hide the pictures or the body copy across every card;
	   they never filter entries out of the feed. All of it is done here rather
	   than in FeedCard so that this one media query makes the whole feature
	   desktop-only: on a phone the rules don't apply, so both chips are moot and
	   everything shows. The chips themselves are hidden below the same
	   breakpoint (routes/feeds/+layout.svelte), which is the shell's own 768px.

	   Keep this query and the chips' `max-width: 768px` in step — if they drift,
	   a phone gets a control that does nothing, or loses one that does. */
  @media (min-width: 769px) {
    .feed-grid.hide-images :global(.card-image),
    .feed-grid.hide-images :global(.card-gallery) {
      display: none;
    }

    /* Body copy arrives as {@html}, so pictures inside it are only reachable
		   from CSS — without this an images-off view is still full of images.
		   Captions survive; an emptied figure loses its margin so it leaves no gap
		   behind. */
    .feed-grid.hide-images :global(.excerpt img),
    .feed-grid.hide-images :global(.excerpt picture) {
      display: none;
    }

    .feed-grid.hide-images :global(.excerpt figure) {
      margin: 0;
    }

    /* A note about withheld repeat images is noise when no images are drawn. */
    .feed-grid.hide-images :global(.repeat-note) {
      display: none;
    }

    .feed-grid.hide-text :global(.excerpt) {
      display: none;
    }
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

  .feed-grid :global(.repeat-note) {
    font-size: var(--text-xs);
    opacity: 0.7;
    white-space: nowrap;
  }

  .feed-grid :global(.meta-link) {
    color: var(--text-dim);
    text-decoration: none;
    margin-left: auto;
  }

  .feed-grid :global(.meta-link:hover) {
    color: #aaa;
  }

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
    bottom: max(0.75rem, var(--safe-bottom));
    right: max(0.75rem, var(--safe-right));
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
    grid-template-rows: auto;
    height: auto;
  }

  .feed-grid.list-view :global(.card-gallery.gallery-3 .card-image:first-child) {
    grid-row: auto;
  }

  .feed-grid.list-view :global(.card-gallery .card-image img) {
    aspect-ratio: auto;
    height: auto;
  }
  :global(:root[data-theme='light']) .feed-grid :global(.card-title-overlay) {
    background: #ececef;
  }
  /* "SEEN" pill flips to a light translucent badge so it reads on light cards
	   and over image thumbnails alike. */
  :global(:root[data-theme='light']) .feed-grid :global(.seen-pill) {
    background: rgba(255, 255, 255, 0.82);
    color: #555;
  }
  :global(:root[data-theme='light']) .feed-grid :global(.meta-link:hover) {
    color: var(--text-primary);
  }
  :global(:root[data-theme='light']) .status-badge {
    background: #ececef;
  }
</style>
