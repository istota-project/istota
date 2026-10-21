<script lang="ts">
  import { createConfirm } from '@minot/ui/ui/confirm.svelte';
  import { latest } from '@minot/ui/ui/latest';
  import { invalidateNavUnreadCounts } from '$lib/stores/navUnread';
  import { onDestroy, onMount, untrack } from 'svelte';
  import { createDrawerClose } from '@minot/ui/ui/drawerClose';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import {
    getBriefingTargets,
    getBriefings,
    getGroupBriefings,
    getGroupEditions,
    type GroupBriefingDefinition,
    type UserBriefingRow,
    type BriefingTarget,
    type GroupEditionSummary,
    getBriefingArchive,
    getBriefingArchiveItem,
    deleteBriefingArchiveItem,
    markBriefingArchiveAsRead,
    type BriefingArchiveItem,
  } from '$lib/api';
  import {
    selectedBriefingId,
    selectedGroupEdition,
    briefingDeleteRequest,
    briefingFilterName,
    briefingArchiveCount,
    briefingArchiveError,
    briefingsRefreshNonce,
    briefingReadAck,
  } from '$lib/stores/briefings';
  import {
    Icon,
    AppShell,
    ShellHeader,
    ListRow,
    Sidebar,
    SidebarNav,
    SidebarToggle,
    CategoryGroup,
    Chip,
    CountPill,
    Select,
    ConfirmDialog,
    EmptyState,
    PageLoading,
    LoadMore,
    Timestamp,
  } from '@minot/ui';
  import { HeaderSave } from '$lib/components/settings';
  import {
    CheckCheck,
    CirclePause,
    Cog,
    Layers,
    PencilLine,
    Plus,
    Send,
    Users,
  } from '@lucide/svelte';
  import { getCurrentUser } from '$lib/userContext';
  import { notifyError } from '@minot/ui/stores/notices';
  import { formatDateTime } from '@minot/ui/dateFormat';
  import { createUrlSelection, type Params } from '$lib/navigation/urlSelection.svelte';

  let { children } = $props();

  const identity = getCurrentUser();
  const PAGE = 20;

  let items = $state<BriefingArchiveItem[]>([]);
  let total = $state(0);
  let targets = $state<BriefingTarget[]>([]);
  let groupItems = $state<GroupEditionSummary[]>([]);
  let groupTotal = $state(0);
  // A display filter over the list, not a selection: it is not in the URL.
  let audience = $state('all');
  const showPersonal = $derived(audience === 'all' || audience === 'user');
  const showGroups = $derived(audience !== 'user' && targets.length > 0);
  type Entry =
    | { kind: 'user'; at: string; item: BriefingArchiveItem }
    | { kind: 'group'; at: string; item: GroupEditionSummary };
  // One list, newest first, with each source's loaded pages interleaved.
  const entries = $derived(
    [
      ...(showPersonal
        ? items.map((item): Entry => ({ kind: 'user', at: item.generated_at, item }))
        : []),
      ...(showGroups
        ? groupItems.map((item): Entry => ({ kind: 'group', at: item.published_at, item }))
        : []),
    ].sort((a, b) => b.at.localeCompare(a.at)),
  );
  const listTotal = $derived((showPersonal ? total : 0) + (showGroups ? groupTotal : 0));
  const hasMore = $derived(
    (showPersonal && items.length < total) || (showGroups && groupItems.length < groupTotal),
  );
  const audienceOptions = $derived([
    { value: 'all', label: 'All' },
    { value: 'user', label: 'Just me' },
    ...targets.map((target) => ({ value: target.group_id!, label: target.label })),
  ]);
  let names = $state<string[]>([]);
  let offset = $state(0);
  let sidebarOpen = $state(false);
  const drawer = createDrawerClose(
    () => sidebarOpen,
    () => (sidebarOpen = false),
  );
  onDestroy(drawer.cancel);
  let loadingMore = $state(false);

  // The archived briefing pending a delete confirmation (null = no dialog).
  const deleteTarget = createConfirm<BriefingArchiveItem>();
  $effect(() => {
    const request = $briefingDeleteRequest;
    if (!request) return;
    deleteTarget.ask(request);
    briefingDeleteRequest.set(null);
  });
  $effect(() => {
    const ack = $briefingReadAck;
    if (!ack) return;
    briefingReadAck.set(null);
    markItemsRead((item) => item.id === ack.id, ack.read_at);
  });

  function markItemsRead(match: (item: BriefingArchiveItem) => boolean, readAt: string) {
    if (!items.some((item) => !item.read_at && match(item))) return;
    items = items.map((item) =>
      !item.read_at && match(item) ? { ...item, read_at: readAt } : item,
    );
  }
  let deleteError = $state('');
  const markAllTarget = createConfirm<{ name: string; username: string }>();
  let markingAllRead = $state(false);
  let mounted = false;

  // Briefing-name filter, auto-populated from the archive's distinct names.
  let nameOptions = $derived([
    { value: '', label: 'All' },
    ...names.map((n) => ({ value: n, label: n })),
  ]);

  let onSettings = $derived(page.url.pathname.startsWith(`${base}/briefings/settings`));

  // The settings sidebar: every briefing the caller can open, under its audience,
  // then the two settings sections that are not one briefing.
  let settingsSidebarOpen = $state(false);
  const settingsDrawer = createDrawerClose(
    () => settingsSidebarOpen,
    () => (settingsSidebarOpen = false),
  );
  onDestroy(settingsDrawer.cancel);
  let personalBriefings = $state<UserBriefingRow[]>([]);
  let groupBriefings = $state<Record<string, GroupBriefingDefinition[]>>({});
  const settingsParams = $derived(onSettings ? page.url.searchParams : new URLSearchParams());
  const settingsView = $derived(settingsParams.get('view') ?? '');
  const settingsUrl = (query: Record<string, string>) =>
    `${base}/briefings/settings/?${new URLSearchParams(query)}`;
  function settingsActive(query: Record<string, string>): boolean {
    const keys = ['view', 'name', 'group', 'briefing', 'new'];
    return keys.every((key) => (settingsParams.get(key) ?? '') === (query[key] ?? ''));
  }
  const settingsIndexLoads = latest();
  async function loadSettingsIndex() {
    const current = settingsIndexLoads.next();
    const [personal, ...groups] = await Promise.allSettled([
      getBriefings(),
      ...targets.map((target) => getGroupBriefings(target.group_id!)),
    ]);
    if (!current()) return;
    if (personal.status === 'fulfilled') personalBriefings = personal.value?.briefings ?? [];
    groupBriefings = Object.fromEntries(
      targets.map((target, index) => {
        const result = groups[index];
        return [target.group_id!, result.status === 'fulfilled' ? (result.value?.items ?? []) : []];
      }),
    );
  }

  let onReader = $derived(
    page.url.pathname.startsWith(`${base}/briefings`) && !onSettings && !$selectedGroupEdition,
  );

  $effect(() => {
    if (
      markAllTarget.target &&
      (!onReader || identity.user.username !== markAllTarget.target.username)
    ) {
      markAllTarget.cancel();
    }
  });

  function handleMarkAllRead() {
    if (markingAllRead || !onReader) return;
    markAllTarget.ask({ name: $briefingFilterName, username: identity.user.username });
  }

  async function performMarkAllRead() {
    const target = markAllTarget.target;
    markAllTarget.cancel();
    if (!target || markingAllRead || !onReader || identity.user.username !== target.username)
      return;
    markingAllRead = true;
    try {
      await markBriefingArchiveAsRead(target.name ? 'briefing' : 'all', target.name || undefined);
      invalidateNavUnreadCounts();
      markItemsRead(
        (item) => !target.name || item.briefing_name === target.name,
        new Date().toISOString(),
      );
    } catch {
      if (mounted && onReader && identity.user.username === target.username) {
        notifyError("Couldn't mark these as read.");
      }
    } finally {
      markingAllRead = false;
    }
  }

  type BriefingSelection = {
    id: number | null;
    name: string;
    group?: string;
    briefing?: string;
    run?: number;
  };
  let initialized = $state(false);
  const archiveLoads = latest();

  function encodeSelection(selection: BriefingSelection): Params {
    const params: Params = {};
    if (selection.group && selection.briefing && selection.run) {
      params.group = selection.group;
      params.briefing = selection.briefing;
      params.run = String(selection.run);
      return params;
    }
    if (selection.id !== null) params.id = String(selection.id);
    if (selection.name) params.name = selection.name;
    return params;
  }

  function readerUrl(selection: BriefingSelection): string {
    const query = new URLSearchParams(encodeSelection(selection)).toString();
    return `${base}/briefings/${query ? `?${query}` : ''}`;
  }

  function applySelection(selection: BriefingSelection): void | Promise<void> {
    selectedGroupEdition.set(
      selection.group && selection.briefing && selection.run
        ? { group: selection.group, briefing: selection.briefing, run: selection.run }
        : null,
    );
    const nameChanged = selection.name !== $briefingFilterName;
    briefingFilterName.set(selection.name);
    selectedBriefingId.set(selection.id);
    if (nameChanged || !items.some((item) => item.id === selection.id)) {
      offset = 0;
      return load();
    }
  }

  const briefingSel = createUrlSelection<BriefingSelection>({
    key: 'briefings',
    params: ['id', 'name', 'group', 'briefing', 'run'],
    encode: encodeSelection,
    decode(params) {
      if (onSettings) return null;
      const id = Number(params.id);
      const run = Number(params.run);
      return {
        id: Number.isSafeInteger(id) && id > 0 ? id : null,
        name: params.name ?? '',
        group: params.group ?? '',
        briefing: params.briefing ?? '',
        run: Number.isSafeInteger(run) && run > 0 ? run : undefined,
      };
    },
    read: () =>
      initialized && !onSettings
        ? $selectedGroupEdition
          ? { id: null, name: $briefingFilterName, ...$selectedGroupEdition }
          : { id: $selectedBriefingId, name: $briefingFilterName }
        : null,
    apply: applySelection,
  });

  // This layout survives settings navigation. A real goto changes page.url;
  // the helper's shallow entries instead change its page.state namespace.
  let lastUrl = page.url;
  $effect(() => {
    const url = page.url;
    if (url === lastUrl) return;
    lastUrl = url;
    untrack(() => {
      if (!initialized || onSettings) return;
      const selection = briefingSel.current();
      if (selection) void applySelection(selection);
    });
  });
  briefingSel.start();

  function toggleSettings() {
    if (onSettings) goto(readerUrl({ id: $selectedBriefingId, name: $briefingFilterName }));
    else goto(`${base}/briefings/settings`);
  }

  async function load(reset = true) {
    const current = archiveLoads.next();
    loadingMore = !reset;
    if (reset) {
      briefingArchiveCount.set(null);
      briefingArchiveError.set(null);
    }
    try {
      const params: Record<string, string> = {
        limit: String(PAGE),
        offset: String(offset),
      };
      if ($briefingFilterName) params.briefing_name = $briefingFilterName;
      const resp = await getBriefingArchive(params);
      if (!current()) return;
      if ($briefingFilterName && !resp.briefing_names.includes($briefingFilterName)) {
        briefingFilterName.set('');
        offset = 0;
        return await load();
      }
      items = reset ? resp.items : [...items, ...resp.items];
      total = resp.total;
      names = resp.briefing_names;
      briefingArchiveCount.set(items.length);
      briefingArchiveError.set(null);
      // Seed a selection so the reader has something to show.
      if (reset) {
        const selectedId = $selectedBriefingId;
        let stillPresent = items.some((i) => i.id === selectedId);
        // An absent row may be older than this page, rather than deleted.
        if (!stillPresent && selectedId !== null) {
          try {
            const selected = await getBriefingArchiveItem(selectedId);
            stillPresent = !$briefingFilterName || selected.briefing_name === $briefingFilterName;
          } catch (error) {
            if (!current() || $selectedBriefingId !== selectedId) return;
            if ((error as { status?: number } | null)?.status !== 404) throw error;
          }
          if (!current() || $selectedBriefingId !== selectedId) return;
        }
        if (!stillPresent) selectedBriefingId.set(items[0]?.id ?? null);
      }
    } catch {
      if (!current()) return;
      // Published rather than swallowed. This used to read "the reader page
      // surfaces its own load errors", which is false for the only case that
      // reaches here: the reader fetches the *selected* briefing, a failed list
      // fetch leaves nothing selected, and its effect returns before its catch.
      // So the count below — zero items, indistinguishable from an empty
      // archive — was the only thing the reader had to go on, and it rendered
      // "No briefings yet" at a user who was offline with briefings configured.
      //
      // A fixed string rather than the thrown message, matching feeds, location
      // and money: offline throws `TypeError: Failed to fetch`, and putting that
      // on the pane is worse than saying plainly what did not happen.
      briefingArchiveCount.set(items.length);
      briefingArchiveError.set('Failed to load briefings');
    } finally {
      if (current()) loadingMore = false;
    }
  }

  function pickName(name: string) {
    const selection = { id: null, name };
    if (onSettings) goto(readerUrl(selection));
    else briefingSel.push(selection);
  }

  function pickItem(id: number) {
    const selection = { id, name: $briefingFilterName };
    drawer.run(() => {
      if (onSettings) goto(readerUrl(selection));
      else briefingSel.push(selection);
    });
  }

  function pickGroupEdition(item: GroupEditionSummary) {
    const selection = {
      id: null,
      name: $briefingFilterName,
      group: item.group_id,
      briefing: item.briefing_name,
      run: item.run_id,
    };
    drawer.run(() => {
      if (onSettings) goto(readerUrl(selection));
      else briefingSel.push(selection);
    });
  }

  const groupLoads = latest();
  async function loadGroupEditions(reset = true) {
    if (audience === 'user' || !targets.length) return;
    const current = groupLoads.next();
    try {
      const response = await getGroupEditions({
        group: audience === 'all' ? undefined : audience,
        limit: PAGE,
        offset: reset ? 0 : groupItems.length,
      });
      if (!current()) return;
      groupItems = reset ? response.items : [...groupItems, ...response.items];
      groupTotal = response.total;
    } catch {
      if (current() && reset) {
        groupItems = [];
        groupTotal = 0;
      }
    }
  }

  function loadMore() {
    if (showPersonal && items.length < total) {
      offset += PAGE;
      void load(false);
    }
    if (showGroups && groupItems.length < groupTotal) void loadGroupEditions(false);
  }

  function chooseAudience(value: string) {
    audience = value;
    groupItems = [];
    groupTotal = 0;
    void loadGroupEditions();
  }

  async function performDelete() {
    const target = deleteTarget.target;
    if (!target) return;
    deleteTarget.cancel();
    deleteError = '';
    try {
      await deleteBriefingArchiveItem(target.id);
      invalidateNavUnreadCounts();
      const idx = items.findIndex((i) => i.id === target.id);
      const wasSelected = $selectedBriefingId === target.id;
      // Optimistic local removal — preserves any already-loaded older pages
      // instead of refetching just the current offset window.
      items = items.filter((i) => i.id !== target.id);
      total = Math.max(0, total - 1);
      briefingArchiveCount.set(items.length);
      if (wasSelected) {
        // Move the reader to a neighbour so it isn't stranded on a dead id.
        const next = items[idx] ?? items[idx - 1] ?? null;
        selectedBriefingId.set(next ? next.id : null);
      }
    } catch (e) {
      deleteError = e instanceof Error ? e.message : 'Failed to delete briefing';
      // Reconcile from the server so the list reflects reality.
      offset = 0;
      void load();
    }
  }

  const fmtDate = (iso: string) => formatDateTime(iso, { dateStyle: 'medium', timeStyle: 'short' });

  // A mail-list row: the briefing's name and when, then who it was for. The
  // edition subject repeats both, so only its part before the date is used.
  function rowView(entry: Entry) {
    if (entry.kind === 'user') {
      const item = entry.item;
      return {
        title: (item.subject || '').split(' — ')[0] || item.briefing_name,
        audience: 'Just me',
        preview: item.preview ?? '',
        unread: !item.read_at,
        active: !$selectedGroupEdition && item.id === $selectedBriefingId,
      };
    }
    const item = entry.item;
    return {
      title: item.briefing_title || (item.subject || '').split(' — ')[0] || item.briefing_name,
      audience: item.group_label,
      preview: item.preview ?? '',
      unread: false,
      active: $selectedGroupEdition?.run === item.run_id,
    };
  }

  // Refresh the archive when the settings page reports a schedule change.
  let lastNonce = 0;
  $effect(() => {
    const n = $briefingsRefreshNonce;
    if (n !== lastNonce) {
      lastNonce = n;
      offset = 0;
      void load();
      void loadGroupEditions();
      if (targetsLoaded) void loadSettingsIndex();
    }
  });
  let targetsLoaded = $state(false);

  onMount(() => {
    mounted = true;
    const selection = briefingSel.current();
    if (selection) void applySelection(selection);
    else {
      selectedGroupEdition.set(null);
      void load();
    }
    void getBriefingTargets()
      .then((response) => {
        targets = response.targets.filter((target) => target.scope === 'group');
        targetsLoaded = true;
        void loadSettingsIndex();
        return loadGroupEditions();
      })
      .catch(() => {});
    initialized = true;
    return () => {
      mounted = false;
      settingsIndexLoads.dispose();
      archiveLoads.dispose();
      groupLoads.dispose();
    };
  });
</script>

<!-- insetBottom only on the settings sub-route. The reader is a card-colored
     surface, so letting the shell hold the bottom inset stops that fill one
     home-indicator's height short of the screen edge and shows a band of the
     shell background under it; the reader pads itself instead (same split the
     chat composer makes). Settings is an ordinary scrolling form with no
     full-bleed surface, so the shell inset is right there. -->
<AppShell insetBottom={onSettings}>
  {#snippet header()}
    <ShellHeader
      title="Briefings"
      onTitleClick={onSettings
        ? () => (settingsSidebarOpen = !settingsSidebarOpen)
        : () => (sidebarOpen = !sidebarOpen)}
      titleActionLabel={onSettings ? 'open settings sections' : 'open archive'}
    >
      {#snippet leading()}
        {#if onSettings}
          <SidebarToggle
            open={settingsSidebarOpen}
            label="Settings"
            onclick={() => (settingsSidebarOpen = !settingsSidebarOpen)}
          />
        {:else}
          <SidebarToggle
            open={sidebarOpen}
            label="Archive"
            count={listTotal}
            onclick={() => (sidebarOpen = !sidebarOpen)}
          />
        {/if}
      {/snippet}
      {#snippet nav()}
        {#if !onSettings && targets.length}
          <Select
            value={audience}
            options={audienceOptions}
            ariaLabel="Filter by audience"
            onValueChange={chooseAudience}
          />
        {/if}
      {/snippet}
      {#snippet tools()}
        {#if !onSettings}
          <Chip icon onclick={() => goto(`${base}/briefings/settings/?new=1`)} title="New briefing">
            <Icon icon={Plus} size="sm" />
          </Chip>
        {/if}
        {#if onReader}
          <Chip
            icon
            onclick={handleMarkAllRead}
            disabled={markingAllRead}
            title={$briefingFilterName
              ? "Mark this briefing's archive as read"
              : 'Mark all briefings as read'}
          >
            <Icon icon={CheckCheck} size="sm" />
          </Chip>
        {/if}
        <!-- Ahead of the cog, so the cog keeps the bar's right edge and stays
			     put whether or not the open page offers a save. Renders nothing
			     unless one is registered. -->
        <HeaderSave />
        <Chip icon checked={onSettings} onclick={toggleSettings} title="Briefing settings">
          <Icon icon={Cog} size="sm" />
        </Chip>
      {/snippet}
    </ShellHeader>
  {/snippet}

  {#snippet sidebar()}
    {#snippet heldMark()}<span class="view-state" title="Paused by an administrator"
        ><Icon icon={CirclePause} size="sm" /><span class="visually-hidden"
          >, paused by an administrator</span
        ></span
      >{/snippet}
    {#snippet offMark()}<span class="view-state" title="Off"
        ><Icon icon={CirclePause} size="sm" /><span class="visually-hidden">, off</span></span
      >{/snippet}
    {#snippet draftMark()}<span class="view-state" title="Draft"
        ><Icon icon={PencilLine} size="sm" /><span class="visually-hidden">, draft</span></span
      >{/snippet}
    {#if onSettings}
      <Sidebar
        title="Settings"
        open={settingsSidebarOpen}
        onClose={() => (settingsSidebarOpen = false)}
      >
        <div class="settings-index">
          <SidebarNav
            items={[
              {
                href: settingsUrl({ new: '1' }),
                label: 'New briefing',
                icon: Plus,
                active: settingsActive({ new: '1' }),
              },
            ]}
            label="New briefing"
            divider
            onnavigate={(item, event) => settingsDrawer.link(event, () => void goto(item.href))}
          />
          <CategoryGroup label="Just me" count={personalBriefings.length}>
            <SidebarNav
              items={personalBriefings.map((item) => ({
                href: settingsUrl({ name: item.name }),
                label: item.title || item.name,
                active: settingsActive({ name: item.name }),
                mark: item.held ? heldMark : !item.enabled ? offMark : undefined,
              }))}
              label="Just me"
              divider
              onnavigate={(item, event) => settingsDrawer.link(event, () => void goto(item.href))}
            />
          </CategoryGroup>
          {#each targets as target (target.group_id)}
            {@const items = groupBriefings[target.group_id!] ?? []}
            {#if items.length || target.capabilities.author_briefings}
              <CategoryGroup label={target.label} count={items.length}>
                <SidebarNav
                  items={items.map((item) => {
                    const query = { group: target.group_id!, briefing: item.name };
                    return {
                      href: settingsUrl(query),
                      label: item.title || item.name,
                      active: settingsActive(query),
                      mark: item.held ? heldMark : !item.enabled ? draftMark : undefined,
                    };
                  })}
                  label={target.label}
                  divider
                  onnavigate={(item, event) =>
                    settingsDrawer.link(event, () => void goto(item.href))}
                />
              </CategoryGroup>
            {/if}
          {/each}
          <SidebarNav
            items={[
              {
                href: settingsUrl({ view: 'delivery' }),
                label: 'Delivery',
                icon: Send,
                active: settingsView === 'delivery',
              },
              ...(identity.user.features?.admin
                ? [
                    {
                      href: settingsUrl({ view: 'shared' }),
                      label: 'Shared blocks',
                      icon: Layers,
                      active: settingsView === 'shared',
                    },
                  ]
                : []),
            ]}
            label="Briefing settings"
            onnavigate={(item, event) => settingsDrawer.link(event, () => void goto(item.href))}
          />
        </div>
      </Sidebar>
    {:else}
      <Sidebar
        title="Archive"
        count={listTotal}
        open={sidebarOpen}
        onClose={() => (sidebarOpen = false)}
      >
        {#if deleteError}
          <p class="sidebar-error">{deleteError}</p>
        {/if}
        {#if entries.length === 0 && showPersonal && $briefingArchiveCount === null}
          <PageLoading />
        {:else if entries.length === 0 && showPersonal && $briefingArchiveError}
          <p class="sidebar-error">{$briefingArchiveError}</p>
        {:else if entries.length === 0}
          <EmptyState size="sm">No briefings yet.</EmptyState>
        {:else}
          {#each entries as entry (entry.kind === 'user' ? `u${entry.item.id}` : `g${entry.item.run_id}`)}
            {@const row = rowView(entry)}
            <ListRow
              class="archive-row"
              active={row.active}
              density="sm"
              onclick={() =>
                entry.kind === 'user' ? pickItem(entry.item.id) : pickGroupEdition(entry.item)}
            >
              <span class="title-card">{row.title}</span>
              {#snippet badge()}
                <Timestamp class="caption num" value={entry.at} format="relative" />
              {/snippet}
              {#snippet meta()}
                <span class="archive-audience caption">
                  {#if entry.kind === 'group'}<Icon icon={Users} size="xs" />{/if}
                  <span class="truncate">{row.audience}</span>
                  <CountPill count={row.unread ? 1 : 0} title="Unread" shape="inline-mark" />
                  {#if row.unread}<span class="visually-hidden">, unread</span>{/if}
                </span>
                {#if row.preview}<span class="archive-preview">{row.preview}</span>{/if}
              {/snippet}
            </ListRow>
          {/each}
          <LoadMore
            onload={loadMore}
            {hasMore}
            loading={loadingMore}
            label="Load older"
            fullWidth
          />
        {/if}
      </Sidebar>
    {/if}
  {/snippet}

  {@render children()}
</AppShell>

{#if markAllTarget.target}
  <ConfirmDialog
    bind:open={markAllTarget.open}
    title="Mark all as read"
    message={markAllTarget.target.name
      ? `Are you sure you want to mark all unread editions of ${markAllTarget.target.name} as read? This can't be undone.`
      : "Are you sure you want to mark all unread briefings as read? This can't be undone."}
    confirmLabel="Mark all read"
    confirmVariant="primary"
    onConfirm={performMarkAllRead}
  />
{/if}

{#if deleteTarget.target}
  <ConfirmDialog
    bind:open={deleteTarget.open}
    title="Delete briefing"
    message={`Permanently remove the archived briefing "${deleteTarget.target.subject || deleteTarget.target.briefing_name}" from ${fmtDate(deleteTarget.target.generated_at)}? This cannot be undone.`}
    confirmLabel="Delete"
    onConfirm={performDelete}
  />
{/if}

<style>
  .archive-audience {
    display: flex;
    align-items: center;
    gap: var(--space-1);
    min-width: 0;
  }

  .archive-preview {
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    overflow: hidden;
    font-size: var(--text-xs);
    line-height: var(--leading-snug);
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }

  /* Pull the unread mark in to 2px from the audience, the feeds sidebar's gap. */
  .archive-audience :global(.count-pill.mark) {
    margin-left: -2px;
  }

  /* Off or draft, as a glyph: a word-sized badge crowded the name. */
  .view-state {
    display: inline-flex;
    flex-shrink: 0;
    color: var(--text-dim);
  }

  /* Group labels start on the same edge as the briefing rows under them. */
  .settings-index :global(.cat-label) {
    padding-left: var(--space-2);
    padding-right: var(--space-2);
  }

  .sidebar-error {
    padding: var(--space-2) var(--space-2);
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
  }
</style>
