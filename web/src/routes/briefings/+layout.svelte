<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { getBriefingArchive, type BriefingArchiveItem } from '$lib/api';
  import {
    selectedBriefingId,
    briefingFilterName,
    briefingArchiveCount,
    briefingsRefreshNonce,
  } from '$lib/stores/briefings';
  import { AppShell, ShellHeader, Sidebar, SidebarToggle, Chip, Select } from '$lib/components/ui';
  import { Cog } from 'lucide-svelte';

  let { children } = $props();

  const PAGE = 20;

  let items = $state<BriefingArchiveItem[]>([]);
  let total = $state(0);
  let names = $state<string[]>([]);
  let offset = $state(0);
  let sidebarOpen = $state(false);
  let loadingMore = $state(false);

  // Briefing-name filter, auto-populated from the archive's distinct names.
  let nameOptions = $derived([
    { value: '', label: 'All' },
    ...names.map((n) => ({ value: n, label: n })),
  ]);

  let onSettings = $derived(page.url.pathname.startsWith(`${base}/briefings/settings`));

  function toggleSettings() {
    if (onSettings) goto(`${base}/briefings`);
    else goto(`${base}/briefings/settings`);
  }

  async function load(reset = true) {
    loadingMore = !reset;
    try {
      const params: Record<string, string> = {
        limit: String(PAGE),
        offset: String(offset),
      };
      if ($briefingFilterName) params.briefing_name = $briefingFilterName;
      const resp = await getBriefingArchive(params);
      items = reset ? resp.items : [...items, ...resp.items];
      total = resp.total;
      names = resp.briefing_names;
      briefingArchiveCount.set(items.length);
      // Seed a selection so the reader has something to show.
      if (reset) {
        const stillPresent = items.some((i) => i.id === $selectedBriefingId);
        if (!stillPresent) selectedBriefingId.set(items[0]?.id ?? null);
      }
    } catch {
      // The reader page surfaces its own load errors; the sidebar just
      // stays empty rather than throwing.
      briefingArchiveCount.set(items.length);
    } finally {
      loadingMore = false;
    }
  }

  function pickName(name: string) {
    briefingFilterName.set(name);
    offset = 0;
    selectedBriefingId.set(null);
    void load();
  }

  function pickItem(id: number) {
    selectedBriefingId.set(id);
    sidebarOpen = false;
    if (onSettings) goto(`${base}/briefings`);
  }

  function loadMore() {
    offset += PAGE;
    void load(false);
  }

  function fmtDate(iso: string): string {
    try {
      return new Date(iso).toLocaleString(undefined, {
        dateStyle: 'medium',
        timeStyle: 'short',
      });
    } catch {
      return iso;
    }
  }

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

  onMount(() => load());
</script>

<AppShell>
  {#snippet header()}
    <ShellHeader title="Briefings">
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
        <Chip icon checked={onSettings} onclick={toggleSettings} title="Briefing settings">
          <Cog size={14} />
        </Chip>
        {#if !onSettings}
          <SidebarToggle
            open={sidebarOpen}
            label="Archive"
            count={total}
            onclick={() => (sidebarOpen = !sidebarOpen)}
          />
        {/if}
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
        {#if items.length === 0}
          <p class="sidebar-empty">No briefings yet.</p>
        {:else}
          {#each items as item (item.id)}
            <button
              class="archive-btn"
              class:active={item.id === $selectedBriefingId}
              type="button"
              onclick={() => pickItem(item.id)}
            >
              <span class="archive-subject">{item.subject || item.briefing_name}</span>
              <span class="archive-date">{fmtDate(item.generated_at)}</span>
            </button>
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

<style>
  .archive-btn {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    width: 100%;
    text-align: left;
    background: none;
    border: none;
    color: inherit;
    font: inherit;
    cursor: pointer;
    padding: 0.4rem 0.5rem;
    border-radius: 0.3rem;
    transition: background var(--transition-fast);
  }

  .archive-btn:hover {
    background: var(--surface-raised);
  }

  .archive-btn.active {
    background: var(--surface-raised);
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
    margin-top: 0.4rem;
    padding: 0.4rem;
    background: none;
    border: 1px solid var(--border-subtle);
    border-radius: 0.3rem;
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
    padding: 0.5rem;
    font-size: var(--text-sm);
    color: var(--text-dim);
  }
</style>
