<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { getFeeds, markAsRead, type Feed } from '$lib/api';
  import {
    feedsList,
    selectedFeedId,
    selectedCategoryId,
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
    Select,
    ConfirmDialog,
  } from '$lib/components/ui';
  import { HeaderSave } from '$lib/components/settings';
  import { LayoutGrid, List, Cog, Star, CheckCheck, Circle } from '@lucide/svelte';
  import { notifyError } from '$lib/stores/notices';
  import { createUrlSelection, type Params } from '$lib/navigation/urlSelection.svelte';

  let { children } = $props();

  const sortOptions = [
    { value: 'published', label: 'Published' },
    { value: 'added', label: 'Added' },
  ];

  let feeds: Feed[] = $derived($feedsList);
  let sidebarOpen = $state(false);

  let onSettings = $derived(page.url.pathname.startsWith(`${base}/feeds/settings`));

  type FeedSelection = { feed: number; category: number; view: '' | 'starred' | 'unread' };
  const all: FeedSelection = { feed: 0, category: 0, view: '' };
  let feedsReady = $state(false);
  let selectionReady = $state(false);
  let mounted = false;
  let loadGeneration = 0;

  function encodeSelection(selection: FeedSelection): Params {
    if (selection.feed) return { feed: String(selection.feed) };
    if (selection.category) return { category: String(selection.category) };
    if (selection.view) return { view: selection.view };
    return {};
  }

  function readSelection(): FeedSelection {
    return {
      feed: $selectedFeedId,
      category: $selectedCategoryId,
      view: $showStarred ? 'starred' : $showUnseen ? 'unread' : '',
    };
  }

  function applySelection(selection: FeedSelection) {
    selectedFeedId.set(selection.feed);
    selectedCategoryId.set(selection.category);
    showStarred.set(selection.view === 'starred');
    showUnseen.set(selection.view === 'unread');
  }

  const feedSel = createUrlSelection<FeedSelection>({
    key: 'feeds',
    params: ['feed', 'category', 'view'],
    encode: encodeSelection,
    decode(params) {
      if (onSettings) return null;
      if (params.feed) {
        if (!feedsReady) return null;
        const id = Number(params.feed);
        return Number.isSafeInteger(id) && id > 0 && feeds.some((feed) => feed.id === id)
          ? { ...all, feed: id }
          : null;
      }
      if (params.category) {
        if (!feedsReady) return null;
        const id = Number(params.category);
        return Number.isSafeInteger(id) && id > 0 && feeds.some((feed) => feed.category.id === id)
          ? { ...all, category: id }
          : null;
      }
      if (params.view === 'starred' || params.view === 'unread') {
        return { ...all, view: params.view };
      }
      return params.view ? null : all;
    },
    read: () => (selectionReady && !onSettings ? readSelection() : null),
    apply: applySelection,
  });

  // Settings shares this layout. Real navigations need fresh subscriptions
  // before validating the query; shallow Back/Forward uses the helper instead.
  let lastUrl = page.url;
  $effect(() => {
    const url = page.url;
    if (url === lastUrl) return;
    lastUrl = url;
    untrack(() => {
      if (!mounted) return;
      if (onSettings) {
        loadGeneration++;
        return;
      }
      void loadFeeds();
    });
  });
  feedSel.start();

  function readerUrl(selection: FeedSelection): string {
    const query = new URLSearchParams(encodeSelection(selection)).toString();
    return `${base}/feeds/${query ? `?${query}` : ''}`;
  }

  function toggleSettings() {
    if (onSettings) goto(readerUrl(readSelection()));
    else goto(`${base}/feeds/settings`);
  }

  function pickSelection(selection: FeedSelection) {
    sidebarOpen = false;
    if (onSettings) goto(readerUrl(selection));
    else {
      selectionReady = true;
      feedSel.push(selection);
    }
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
    pickSelection({ ...all, feed: $selectedFeedId === feedId ? 0 : feedId });
  }

  function handleCategoryClick(categoryId: number) {
    pickSelection({ ...all, category: $selectedCategoryId === categoryId ? 0 : categoryId });
  }

  function handleAllClick() {
    pickSelection(all);
  }

  function handleUnreadClick() {
    pickSelection({ ...all, view: 'unread' });
  }

  function handleStarredClick() {
    pickSelection({ ...all, view: 'starred' });
  }

  let markAllTargetTitle = $state<string | null>(null);

  function handleMarkAllRead() {
    markAllTargetTitle = $selectedFeedId
      ? feeds.find((f) => f.id === $selectedFeedId)?.title || 'this feed'
      : $selectedCategoryId
        ? feeds.find((f) => f.category.id === $selectedCategoryId)?.category.title ||
          'this category'
        : 'every feed';
  }

  async function performMarkAllRead() {
    const scope = $selectedFeedId ? 'feed' : $selectedCategoryId ? 'category' : 'all';
    const scopeId = $selectedFeedId || $selectedCategoryId || 0;
    markAllTargetTitle = null;
    try {
      await markAsRead(scope, scopeId ? { id: scopeId } : undefined);
      feedsRefreshNonce.update((n) => n + 1);
    } catch (e) {
      console.error('mark-all-read failed', e);
      notifyError("Couldn't mark these as read.");
    }
  }

  async function loadFeeds() {
    const generation = ++loadGeneration;
    feedsReady = false;
    selectionReady = false;
    try {
      const data = await getFeeds({ limit: '1', offset: '0' });
      if (generation !== loadGeneration) return;
      feedsList.set(data.feeds);
      feedsReady = true;
      if (!onSettings) applySelection(feedSel.current() ?? all);
      selectionReady = true;
    } catch {
      if (generation !== loadGeneration) return;
      // Views do not need subscriptions. Keep unresolved ids in the URL.
      const selection = feedSel.current();
      if (selection) {
        applySelection(selection);
        selectionReady = true;
      }
    }
  }

  onMount(() => {
    mounted = true;
    void loadFeeds();
    return () => {
      mounted = false;
      loadGeneration++;
    };
  });
</script>

<AppShell>
  {#snippet header()}
    <ShellHeader
      title="Feeds"
      onTitleClick={onSettings ? undefined : () => (sidebarOpen = !sidebarOpen)}
      titleActionLabel="open sources"
    >
      {#snippet leading()}
        {#if !onSettings}
          <SidebarToggle
            open={sidebarOpen}
            label="Sources"
            count={feeds.length}
            onclick={() => (sidebarOpen = !sidebarOpen)}
          />
        {/if}
      {/snippet}
      {#snippet nav()}
        {#if !onSettings}
          <Select
            value={$sortBy}
            options={sortOptions}
            onValueChange={(v) => sortBy.set(v as 'published' | 'added')}
            ariaLabel="Sort order"
          />
          <div class="filter-group">
            <Chip checked={$showImages} onclick={() => showImages.update((v) => !v)}>Images</Chip>
            <Chip checked={$showText} onclick={() => showText.update((v) => !v)}>Text</Chip>
          </div>
        {/if}
      {/snippet}
      {#snippet tools()}
        {#if !onSettings}
          <Chip
            icon
            checked={$viewMode === 'grid'}
            onclick={() => viewMode.set('grid')}
            title="Grid view"
          >
            <LayoutGrid size={14} />
          </Chip>
          <Chip
            icon
            checked={$viewMode === 'list'}
            onclick={() => viewMode.set('list')}
            title="List view"
          >
            <List size={14} />
          </Chip>
          <Chip
            icon
            onclick={handleMarkAllRead}
            title={$selectedFeedId ? 'Mark this feed as read' : 'Mark every feed as read'}
          >
            <CheckCheck size={14} />
          </Chip>
        {/if}
        <!-- Ahead of the cog, so the cog keeps the bar's right edge and stays
			     put whether or not the open page offers a save. Renders nothing
			     unless one is registered. -->
        <HeaderSave />
        <Chip icon checked={onSettings} onclick={toggleSettings} title="Feed settings">
          <Cog size={14} />
        </Chip>
      {/snippet}
    </ShellHeader>
  {/snippet}

  {#snippet sidebar()}
    {#if !onSettings}
      <Sidebar
        title="Sources"
        count={feeds.length}
        open={sidebarOpen}
        onClose={() => (sidebarOpen = false)}
      >
        <!-- Cross-feed views, above the feed list. Mirrors the chat sidebar's
			     All / Unread / Starred entries so the two read the same. -->
        <div class="views">
          <button
            class="view-btn"
            class:active={!$selectedFeedId && !$selectedCategoryId && !$showStarred && !$showUnseen}
            onclick={handleAllClick}
            type="button"
          >
            <span class="view-name">All</span>
          </button>
          <button
            class="view-btn"
            class:active={$showUnseen && !$showStarred && !$selectedFeedId}
            onclick={handleUnreadClick}
            type="button"
          >
            <Circle size={12} />
            <span class="view-name">Unread</span>
          </button>
          <button
            class="view-btn"
            class:active={$showStarred}
            onclick={handleStarredClick}
            type="button"
          >
            <Star size={12} />
            <span class="view-name">Starred</span>
          </button>
        </div>
        {#each groupedFeeds as [category, catFeeds] (category)}
          {@const catId = catFeeds[0]?.category.id ?? 0}
          <CategoryGroup
            label={category}
            count={catFeeds.length}
            collapsible
            active={catId !== 0 && $selectedCategoryId === catId}
            onSelect={catId !== 0 ? () => handleCategoryClick(catId) : undefined}
          >
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
    {/if}
  {/snippet}

  {@render children()}
</AppShell>

<ConfirmDialog
  open={markAllTargetTitle != null}
  title="Mark all as read"
  message={`Are you sure you want to mark all unread entries in ${markAllTargetTitle} as read? This can't be undone.`}
  confirmLabel="Mark all read"
  confirmVariant="primary"
  onConfirm={performMarkAllRead}
  onCancel={() => (markAllTargetTitle = null)}
/>

<style>
  .filter-group {
    display: flex;
    gap: var(--chip-gap);
    /* Preserve the separation the sort dropdown's old margin-right gave. */
    margin-left: var(--space-2);
  }

  /* The Images / Text chips are a desktop affordance: on a phone a feed is a
	   single column you scroll, so suppressing pictures or body copy buys little
	   and the header has no room to spare. The rules they drive are scoped to the
	   matching `min-width: 769px` query in +page.svelte, so hiding the chips here
	   leaves no orphaned state — a phone always shows everything, whatever the
	   stored toggles say, and the desktop view is unchanged when you go back. */
  @media (max-width: 768px) {
    .filter-group {
      display: none;
    }
  }

  .feed-btn {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    width: 100%;
    background: none;
    border: none;
    color: inherit;
    font: inherit;
    cursor: pointer;
    padding: var(--space-1) var(--space-3);
    border-radius: var(--radius-sm);
    transition: background var(--transition-fast);
    text-align: left;
  }

  /* .views / .view-btn / .view-name (the All / Unread / Starred block) come
	   from web/src/lib/styles/sidebar.css, shared with the chat sidebar. */
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
