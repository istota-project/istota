<script lang="ts">
  import { createConfirm } from '$lib/ui/confirm.svelte';
  import { latest } from '$lib/ui/latest';
  import { invalidateNavUnreadCounts } from '$lib/stores/navUnread';
  import { onDestroy, onMount, untrack } from 'svelte';
  import { createDrawerClose } from '$lib/ui/drawerClose';
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
    feedCounts,
    refreshFeedCounts,
  } from '$lib/stores/feeds';
  import {
    Icon,
    AppShell,
    ShellHeader,
    Sidebar,
    SidebarToggle,
    CategoryGroup,
    ListRow,
    Chip,
    ToggleGroup,
    Select,
    ConfirmDialog,
    CountPill,
  } from '$lib/components/ui';
  import { HeaderSave } from '$lib/components/settings';
  import { LayoutGrid, List, Cog, CheckCheck } from '@lucide/svelte';
  import ViewFilters from '$lib/components/ViewFilters.svelte';
  import { notifyError } from '$lib/stores/notices';
  import { createUrlSelection, type Params } from '$lib/navigation/urlSelection.svelte';

  let { children } = $props();

  const sortOptions = [
    { value: 'published', label: 'Published' },
    { value: 'added', label: 'Added' },
  ];

  let feeds: Feed[] = $derived($feedsList);
  let sidebarOpen = $state(false);
  const drawer = createDrawerClose(
    () => sidebarOpen,
    () => (sidebarOpen = false),
  );
  onDestroy(drawer.cancel);

  let onSettings = $derived(page.url.pathname.startsWith(`${base}/feeds/settings`));

  type FeedSelection = { feed: number; category: number; view: '' | 'starred' | 'unread' };
  const all: FeedSelection = { feed: 0, category: 0, view: '' };
  let feedsReady = $state(false);
  let selectionReady = $state(false);
  let mounted = false;
  const feedLoads = latest();

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
        feedLoads.invalidate();
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
    drawer.run(() => {
      if (onSettings) goto(readerUrl(selection));
      else {
        selectionReady = true;
        feedSel.push(selection);
      }
    });
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

  const markAllTargetTitle = createConfirm<string>();

  function handleMarkAllRead() {
    markAllTargetTitle.ask(
      $selectedFeedId
        ? feeds.find((f) => f.id === $selectedFeedId)?.title || 'this feed'
        : $selectedCategoryId
          ? feeds.find((f) => f.category.id === $selectedCategoryId)?.category.title ||
            'this category'
          : 'every feed',
    );
  }

  async function performMarkAllRead() {
    const scope = $selectedFeedId ? 'feed' : $selectedCategoryId ? 'category' : 'all';
    const scopeId = $selectedFeedId || $selectedCategoryId || 0;
    markAllTargetTitle.cancel();
    try {
      await markAsRead(scope, scopeId ? { id: scopeId } : undefined);
      invalidateNavUnreadCounts();
      void refreshFeedCounts();
      feedsRefreshNonce.update((n) => n + 1);
    } catch (e) {
      console.error('mark-all-read failed', e);
      notifyError("Couldn't mark these as read.");
    }
  }

  async function loadFeeds() {
    const current = feedLoads.next();
    feedsReady = false;
    selectionReady = false;
    try {
      const data = await getFeeds({ limit: '1', offset: '0' });
      if (!current()) return;
      feedsList.set(data.feeds);
      feedsReady = true;
      if (!onSettings) applySelection(feedSel.current() ?? all);
      selectionReady = true;
    } catch {
      if (!current()) return;
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
    void refreshFeedCounts();
    return () => {
      mounted = false;
      feedLoads.dispose();
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
          <ToggleGroup
            label="Layout"
            iconOnly
            value={$viewMode}
            onValueChange={(mode) => viewMode.set(mode as 'grid' | 'list')}
            options={[
              { value: 'grid', label: 'Grid view', icon: LayoutGrid },
              { value: 'list', label: 'List view', icon: List },
            ]}
          />
          <Chip
            icon
            onclick={handleMarkAllRead}
            title={$selectedFeedId ? 'Mark this feed as read' : 'Mark every feed as read'}
          >
            <Icon icon={CheckCheck} size="sm" />
          </Chip>
        {/if}
        <!-- Ahead of the cog, so the cog keeps the bar's right edge and stays
			     put whether or not the open page offers a save. Renders nothing
			     unless one is registered. -->
        <HeaderSave />
        <Chip icon checked={onSettings} onclick={toggleSettings} title="Feed settings">
          <Icon icon={Cog} size="sm" />
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
        <ViewFilters
          current={!$selectedFeedId && !$selectedCategoryId && !$showStarred && !$showUnseen
            ? 'all'
            : $showStarred
              ? 'starred'
              : $showUnseen && !$selectedFeedId
                ? 'unread'
                : null}
          href={(v) => readerUrl({ ...all, view: v === 'all' ? '' : v })}
          onselect={(v) => pickSelection({ ...all, view: v === 'all' ? '' : v })}
          unread={$feedCounts.unread}
          starred={$feedCounts.starred}
        />
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
              {@const unread = $feedCounts.unread_by_feed[feed.id] ?? 0}
              {#snippet unreadMark()}
                <CountPill count={unread} title="Unread" shape="inline-mark" /><span
                  class="visually-hidden">, unread</span
                >
              {/snippet}
              <ListRow
                density="sm"
                active={$selectedFeedId === feed.id}
                onclick={() => handleFeedClick(feed.id)}
                mark={unread > 0 ? unreadMark : undefined}
              >
                <span class:unread={unread > 0}>{feed.title}</span>
              </ListRow>
            {/each}
          </CategoryGroup>
        {/each}
      </Sidebar>
    {/if}
  {/snippet}

  {@render children()}
</AppShell>

<ConfirmDialog
  bind:open={markAllTargetTitle.open}
  title="Mark all as read"
  message={`Are you sure you want to mark all unread entries in ${markAllTargetTitle.target} as read? This can't be undone.`}
  confirmLabel="Mark all read"
  confirmVariant="primary"
  onConfirm={performMarkAllRead}
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

  /* A source with unread entries reads bolder, as a chat room does. */
  .unread {
    font-weight: var(--weight-semibold);
    color: var(--text-primary);
  }
</style>
