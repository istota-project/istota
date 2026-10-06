<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import {
    getBriefingArchive,
    getBriefingArchiveItem,
    deleteBriefingArchiveItem,
    type BriefingArchiveItem,
  } from '$lib/api';
  import {
    selectedBriefingId,
    briefingFilterName,
    briefingArchiveCount,
    briefingArchiveError,
    briefingsRefreshNonce,
  } from '$lib/stores/briefings';
  import {
    AppShell,
    ShellHeader,
    Sidebar,
    SidebarToggle,
    Chip,
    Select,
    KebabMenu,
    ConfirmDialog,
  } from '$lib/components/ui';
  import { HeaderSave } from '$lib/components/settings';
  import { Cog } from '@lucide/svelte';
  import { formatDateTime } from '$lib/dateFormat';
  import { createUrlSelection, type Params } from '$lib/navigation/urlSelection.svelte';

  let { children } = $props();

  const PAGE = 20;

  let items = $state<BriefingArchiveItem[]>([]);
  let total = $state(0);
  let names = $state<string[]>([]);
  let offset = $state(0);
  let sidebarOpen = $state(false);
  let loadingMore = $state(false);

  // The archived briefing pending a delete confirmation (null = no dialog).
  let deleteTarget = $state<BriefingArchiveItem | null>(null);
  let deleteError = $state('');

  // Briefing-name filter, auto-populated from the archive's distinct names.
  let nameOptions = $derived([
    { value: '', label: 'All' },
    ...names.map((n) => ({ value: n, label: n })),
  ]);

  let onSettings = $derived(page.url.pathname.startsWith(`${base}/briefings/settings`));

  type BriefingSelection = { id: number | null; name: string };
  let initialized = $state(false);
  let loadGeneration = 0;

  function encodeSelection(selection: BriefingSelection): Params {
    const params: Params = {};
    if (selection.id !== null) params.id = String(selection.id);
    if (selection.name) params.name = selection.name;
    return params;
  }

  function readerUrl(selection: BriefingSelection): string {
    const query = new URLSearchParams(encodeSelection(selection)).toString();
    return `${base}/briefings/${query ? `?${query}` : ''}`;
  }

  function applySelection(selection: BriefingSelection): void | Promise<void> {
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
    params: ['id', 'name'],
    encode: encodeSelection,
    decode(params) {
      if (onSettings) return null;
      const id = Number(params.id);
      return {
        id: Number.isSafeInteger(id) && id > 0 ? id : null,
        name: params.name ?? '',
      };
    },
    read: () =>
      initialized && !onSettings ? { id: $selectedBriefingId, name: $briefingFilterName } : null,
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
    if (onSettings) goto(readerUrl(selection));
    else briefingSel.push(selection);
  }

  function pickItem(id: number) {
    sidebarOpen = false;
    const selection = { id, name: $briefingFilterName };
    if (onSettings) goto(readerUrl(selection));
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
    const selection = briefingSel.current();
    if (selection) void applySelection(selection);
    else void load();
    initialized = true;
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
        <!-- Ahead of the cog, so the cog keeps the bar's right edge and stays
			     put whether or not the open page offers a save. Renders nothing
			     unless one is registered. -->
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
        {#if deleteError}
          <p class="sidebar-error">{deleteError}</p>
        {/if}
        {#if items.length === 0 && $briefingArchiveError}
          <p class="sidebar-error">{$briefingArchiveError}</p>
        {:else if items.length === 0}
          <p class="sidebar-empty">No briefings yet.</p>
        {:else}
          {#each items as item (item.id)}
            <div class="list-row archive-row" class:active={item.id === $selectedBriefingId}>
              <button class="archive-btn" type="button" onclick={() => pickItem(item.id)}>
                <span class="archive-subject">{item.subject || item.briefing_name}</span>
                <span class="archive-date">{fmtDate(item.generated_at)}</span>
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
      </Sidebar>
    {/if}
  {/snippet}

  {@render children()}
</AppShell>

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
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
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

  .sidebar-empty {
    padding: var(--space-2);
    font-size: var(--text-sm);
    color: var(--text-dim);
  }

  .sidebar-error {
    padding: var(--space-2) var(--space-2);
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
  }
</style>
