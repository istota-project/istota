<script lang="ts">
  import { invalidateNavUnreadCounts } from '$lib/stores/navUnread';
  import { onMount, untrack } from 'svelte';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import {
    getBriefingTargets,
    getGroupBriefings,
    type BriefingTarget,
    type GroupBriefingDefinition,
    getBriefingArchive,
    getBriefingArchiveItem,
    deleteBriefingArchiveItem,
    markBriefingArchiveAsRead,
    type BriefingArchiveItem,
  } from '$lib/api';
  import {
    selectedBriefingId,
    selectedBriefingGroup,
    briefingFilterName,
    briefingArchiveCount,
    briefingArchiveError,
    briefingsRefreshNonce,
  } from '$lib/stores/briefings';
  import {
    AppShell,
    Button,
    ShellHeader,
    Sidebar,
    SidebarToggle,
    Chip,
    Select,
    KebabMenu,
    ConfirmDialog,
    EmptyState,
    PageLoading,
  } from '$lib/components/ui';
  import { HeaderSave } from '$lib/components/settings';
  import { CheckCheck, Cog } from '@lucide/svelte';
  import { getCurrentUser } from '$lib/userContext';
  import { notifyError } from '$lib/stores/notices';
  import { formatDateTime } from '$lib/dateFormat';
  import { createUrlSelection, type Params } from '$lib/navigation/urlSelection.svelte';

  let { children } = $props();

  const identity = getCurrentUser();
  const PAGE = 20;

  let items = $state<BriefingArchiveItem[]>([]);
  let total = $state(0);
  let targets = $state<BriefingTarget[]>([]);
  let groupDefinitions = $state<GroupBriefingDefinition[]>([]);
  let audience = $state('all');
  const audienceOptions = $derived([
    { value: 'all', label: 'All' },
    { value: 'user', label: 'Just me' },
    ...targets.map((target) => ({ value: target.group_id!, label: target.label })),
  ]);
  let names = $state<string[]>([]);
  let offset = $state(0);
  let sidebarOpen = $state(false);
  let loadingMore = $state(false);

  // The archived briefing pending a delete confirmation (null = no dialog).
  let deleteTarget = $state<BriefingArchiveItem | null>(null);
  let deleteError = $state('');
  let markAllTarget = $state<{ name: string; username: string } | null>(null);
  let markingAllRead = $state(false);
  let mounted = false;

  // Briefing-name filter, auto-populated from the archive's distinct names.
  let nameOptions = $derived([
    { value: '', label: 'All' },
    ...names.map((n) => ({ value: n, label: n })),
  ]);

  let onSettings = $derived(page.url.pathname.startsWith(`${base}/briefings/settings`));

  let onReader = $derived(
    page.url.pathname.startsWith(`${base}/briefings`) && !onSettings && !$selectedBriefingGroup,
  );

  $effect(() => {
    if (markAllTarget && (!onReader || identity.user.username !== markAllTarget.username)) {
      markAllTarget = null;
    }
  });

  function handleMarkAllRead() {
    if (markingAllRead || !onReader) return;
    markAllTarget = { name: $briefingFilterName, username: identity.user.username };
  }

  async function performMarkAllRead() {
    const target = markAllTarget;
    markAllTarget = null;
    if (!target || markingAllRead || !onReader || identity.user.username !== target.username)
      return;
    markingAllRead = true;
    try {
      await markBriefingArchiveAsRead(target.name ? 'briefing' : 'all', target.name || undefined);
      invalidateNavUnreadCounts();
    } catch {
      if (mounted && onReader && identity.user.username === target.username) {
        notifyError("Couldn't mark these as read.");
      }
    } finally {
      markingAllRead = false;
    }
  }

  type BriefingSelection = { id: number | null; name: string; group?: string };
  let initialized = $state(false);
  let loadGeneration = 0;

  function encodeSelection(selection: BriefingSelection): Params {
    const params: Params = {};
    if (selection.group) {
      params.group = selection.group;
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
    selectedBriefingGroup.set(selection.group ?? '');
    if (selection.group) audience = selection.group;
    else if (audience !== 'user') audience = 'all';
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
    params: ['id', 'name', 'group'],
    encode: encodeSelection,
    decode(params) {
      if (onSettings) return null;
      const id = Number(params.id);
      return {
        id: Number.isSafeInteger(id) && id > 0 ? id : null,
        name: params.name ?? '',
        group: params.group ?? '',
      };
    },
    read: () =>
      initialized && !onSettings
        ? {
            id: $selectedBriefingGroup ? null : $selectedBriefingId,
            name: $briefingFilterName,
            group: $selectedBriefingGroup,
          }
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
    const generation = ++loadGeneration;
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
      if (generation !== loadGeneration) return;
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
            if (generation !== loadGeneration || $selectedBriefingId !== selectedId) return;
            if ((error as { status?: number } | null)?.status !== 404) throw error;
          }
          if (generation !== loadGeneration || $selectedBriefingId !== selectedId) return;
        }
        if (!stillPresent) selectedBriefingId.set(items[0]?.id ?? null);
      }
    } catch {
      if (generation !== loadGeneration) return;
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
      if (generation === loadGeneration) loadingMore = false;
    }
  }

  function pickName(name: string) {
    const selection = { id: null, name };
    if (onSettings || $selectedBriefingGroup) goto(readerUrl(selection));
    else briefingSel.push(selection);
  }

  function pickItem(id: number) {
    sidebarOpen = false;
    const selection = { id, name: $briefingFilterName };
    if (onSettings || $selectedBriefingGroup) goto(readerUrl(selection));
    else briefingSel.push(selection);
  }

  function loadMore() {
    offset += PAGE;
    void load(false);
  }

  async function performDelete() {
    const target = deleteTarget;
    if (!target) return;
    deleteTarget = null;
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

  // Refresh the archive when the settings page reports a schedule change.
  let lastNonce = 0;
  $effect(() => {
    const n = $briefingsRefreshNonce;
    if (n !== lastNonce) {
      lastNonce = n;
      offset = 0;
      void load();
    }
  });

  onMount(() => {
    mounted = true;
    const selection = briefingSel.current();
    if (selection) void applySelection(selection);
    else {
      selectedBriefingGroup.set('');
      void load();
    }
    void getBriefingTargets()
      .then(async (response) => {
        targets = response.targets.filter((target) => target.scope === 'group');
        const result = await Promise.allSettled(
          targets.map((target) => getGroupBriefings(target.group_id!)),
        );
        groupDefinitions = result.flatMap((item) =>
          item.status === 'fulfilled' ? item.value.items : [],
        );
      })
      .catch(() => {});
    initialized = true;
    return () => {
      mounted = false;
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
      onTitleClick={onSettings ? undefined : () => (sidebarOpen = !sidebarOpen)}
      titleActionLabel="open archive"
    >
      {#snippet leading()}
        {#if !onSettings}
          <SidebarToggle
            open={sidebarOpen}
            label="Archive"
            count={total}
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
            onValueChange={(value) => {
              audience = value;
              if (value === 'all' || value === 'user') {
                selectedBriefingGroup.set('');
                void goto(readerUrl({ id: $selectedBriefingId, name: $briefingFilterName }));
              } else void goto(readerUrl({ id: null, name: '', group: value }));
            }}
          />
        {/if}
        {#if !onSettings && names.length > 1}
          <Select
            value={$briefingFilterName}
            options={nameOptions}
            onValueChange={(v) => pickName(v)}
            ariaLabel="Filter by briefing"
          />
        {/if}
      {/snippet}
      {#snippet tools()}
        {#if onReader}
          <Chip
            icon
            onclick={handleMarkAllRead}
            disabled={markingAllRead}
            title={$briefingFilterName
              ? "Mark this briefing's archive as read"
              : 'Mark all briefings as read'}
          >
            <CheckCheck size={14} />
          </Chip>
        {/if}
        <!-- Ahead of the cog, so the cog keeps the bar's right edge and stays
			     put whether or not the open page offers a save. Renders nothing
			     unless one is registered. -->
        {#if !onSettings}<Button href={`${base}/briefings/settings?new=1`} size="sm"
            >New briefing</Button
          >{/if}
        <HeaderSave />
        <Chip icon checked={onSettings} onclick={toggleSettings} title="Briefing settings">
          <Cog size={14} />
        </Chip>
      {/snippet}
    </ShellHeader>
  {/snippet}

  {#snippet sidebar()}
    {#if !onSettings}
      <Sidebar
        title="Archive"
        count={total}
        open={sidebarOpen}
        onClose={() => (sidebarOpen = false)}
      >
        {#if audience !== 'user'}
          {#each groupDefinitions.filter((item) => audience === 'all' || item.group_id === audience) as item (`${item.group_id}:${item.id}`)}
            <a
              class="archive-btn"
              href={`${base}/briefings/?group=${encodeURIComponent(item.group_id)}&briefing=${encodeURIComponent(item.name)}`}
            >
              <span class="archive-subject">{item.title || item.name}</span>
              <span class="archive-date"
                >{targets.find((target) => target.group_id === item.group_id)?.label} · {item.channel}</span
              >
              <span class="archive-date"
                >{item.enabled ? item.cron : 'Disabled'} · {item.timezone}</span
              >
              <span class="archive-date"
                >Next: {item.next_run ? fmtDate(item.next_run) : 'None'} · Last: {item.last_run
                  ?.state ?? 'None'}</span
              >
            </a>
          {/each}
        {/if}
        {#if audience === 'all' || audience === 'user'}
          {#if deleteError}
            <p class="sidebar-error">{deleteError}</p>
          {/if}
          {#if items.length === 0 && $briefingArchiveCount === null}
            <PageLoading />
          {:else if items.length === 0 && $briefingArchiveError}
            <p class="sidebar-error">{$briefingArchiveError}</p>
          {:else if items.length === 0}
            <EmptyState size="sm">No briefings yet.</EmptyState>
          {:else}
            {#each items as item (item.id)}
              <div class="list-row archive-row" class:active={item.id === $selectedBriefingId}>
                <button class="archive-btn" type="button" onclick={() => pickItem(item.id)}>
                  <span class="archive-subject">{item.subject || item.briefing_name}</span>
                  <span class="archive-date">Just me · {fmtDate(item.generated_at)}</span>
                </button>
                <KebabMenu
                  ariaLabel="Briefing actions"
                  items={[{ label: 'Delete', danger: true, onSelect: () => (deleteTarget = item) }]}
                />
              </div>
            {/each}
            {#if items.length < total}
              <button class="load-more" type="button" onclick={loadMore} disabled={loadingMore}>
                {loadingMore ? 'Loading…' : 'Load older'}
              </button>
            {/if}
          {/if}
        {/if}
      </Sidebar>
    {/if}
  {/snippet}

  {@render children()}
</AppShell>

{#if markAllTarget}
  <ConfirmDialog
    open={true}
    title="Mark all as read"
    message={markAllTarget.name
      ? `Are you sure you want to mark all unread editions of ${markAllTarget.name} as read? This can't be undone.`
      : "Are you sure you want to mark all unread briefings as read? This can't be undone."}
    confirmLabel="Mark all read"
    confirmVariant="primary"
    onConfirm={performMarkAllRead}
    onCancel={() => (markAllTarget = null)}
  />
{/if}

{#if deleteTarget}
  <ConfirmDialog
    open={true}
    title="Delete briefing"
    message={`Permanently remove the archived briefing "${deleteTarget.subject || deleteTarget.briefing_name}" from ${fmtDate(deleteTarget.generated_at)}? This cannot be undone.`}
    confirmLabel="Delete"
    onConfirm={performDelete}
    onCancel={() => (deleteTarget = null)}
  />
{/if}

<style>
  /* Row = clickable title button (flex:1) + a kebab sibling. A KebabMenu is
     itself a <button>, so it can't be nested inside .archive-btn; the row's
     layout and hover come from `.sidebar .list-row` in lib/styles/sidebar.css,
     shared with the chat sidebar. */
  .archive-btn {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    flex: 1;
    min-width: 0;
    text-align: left;
    background: none;
    border: none;
    color: inherit;
    font: inherit;
    cursor: pointer;
    padding: var(--space-2) var(--space-2);
    border-radius: var(--radius-sm);
  }

  .archive-row.active .archive-btn {
    color: var(--text-primary);
  }

  .archive-subject {
    font-size: var(--text-sm);
    font-weight: 500;
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    white-space: normal;
    line-height: 1.4;
    min-height: 2.8em;
    overflow: hidden;
    overflow-wrap: anywhere;
  }

  .archive-date {
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .load-more {
    width: 100%;
    margin-top: var(--space-2);
    padding: var(--space-2);
    background: none;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    font: inherit;
    font-size: var(--text-xs);
    cursor: pointer;
  }

  .load-more:hover:not(:disabled) {
    background: var(--surface-raised);
    color: var(--text-primary);
  }

  .sidebar-error {
    padding: var(--space-2) var(--space-2);
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
  }
</style>
