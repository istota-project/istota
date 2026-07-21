<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    getFeedsConfig,
    putFeedsConfig,
    importOpml,
    exportOpmlUrl,
    refreshFeeds,
    getModuleServices,
    type FeedsConfigPayload,
    type FeedsConfigFeed,
    type FeedsConfigCategory,
    type FeedsDiagnostics,
    type FeedsFeedState,
    type ServiceCard as ServiceCardData,
  } from '$lib/api';
  import { Button, Modal, ConfirmDialog, Select, type SelectOption } from '$lib/components/ui';
  import {
    ServiceCard,
    SettingsLayout,
    SettingsCard,
    SettingsField,
  } from '$lib/components/settings';

  let loading = $state(true);
  let saving = $state(false);
  let importing = $state(false);
  let refreshing = $state(false);
  let error = $state('');
  let info = $state('');

  let config: FeedsConfigPayload = $state({
    settings: {},
    categories: [],
    feeds: [],
  });
  let diagnostics: FeedsDiagnostics | null = $state(null);
  let feedStateByUrl: Record<string, FeedsFeedState> = $state({});
  let moduleServices: ServiceCardData[] = $state([]);
  let moduleEnabled = $state(true);

  // Track whether the loaded config has been mutated (dirty flag).
  let initialJson = $state('');
  let dirty = $derived(JSON.stringify(config) !== initialJson);

  type FeedDraft = {
    url: string;
    title: string;
    category: string;
    poll_interval_minutes: string; // raw input — empty = inherit default
  };
  let editing: { idx: number; draft: FeedDraft } | null = $state(null);
  let adding: FeedDraft | null = $state(null);
  let modalError = $state('');
  let confirmDelete: { kind: 'feed' | 'category'; idx: number; label: string } | null =
    $state(null);

  let fileInput: HTMLInputElement | undefined = $state();

  const categorySelectOptions: SelectOption[] = $derived([
    { value: '', label: '(none)' },
    ...categoryOptions().map((slug) => ({ value: slug, label: slug })),
  ]);

  async function refresh() {
    loading = true;
    error = '';
    try {
      // Module endpoint first — if the user has feeds disabled we skip
      // the heavier config read and render a banner instead.
      const mod = await getModuleServices('feeds');
      moduleEnabled = mod.module_enabled;
      moduleServices = mod.services;
      if (!moduleEnabled) {
        return;
      }
      const data = await getFeedsConfig();
      config = data.config;
      diagnostics = data.diagnostics;
      feedStateByUrl = Object.fromEntries(data.feed_state.map((s) => [s.url, s]));
      initialJson = JSON.stringify(config);
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load settings';
    } finally {
      loading = false;
    }
  }

  async function reloadServices() {
    try {
      const mod = await getModuleServices('feeds');
      moduleServices = mod.services;
      moduleEnabled = mod.module_enabled;
    } catch {
      // non-fatal — the panel just won't update its configured state
    }
  }

  onMount(refresh);

  async function save() {
    saving = true;
    error = '';
    info = '';
    try {
      const result = await putFeedsConfig(config);
      info = `Saved (${result.sync.feeds_added} added, ${result.sync.feeds_updated} updated, ${result.sync.categories_added} new categories).`;
      initialJson = JSON.stringify(config);
      // Pull diagnostics back so last_poll_at etc. update
      const data = await getFeedsConfig();
      diagnostics = data.diagnostics;
      feedStateByUrl = Object.fromEntries(data.feed_state.map((s) => [s.url, s]));
    } catch (e) {
      error = e instanceof Error ? e.message : 'Save failed';
    } finally {
      saving = false;
    }
  }

  function detectSourceType(url: string | undefined | null): string {
    if (!url) return '?';
    const u = url.trim().toLowerCase();
    if (u.startsWith('tumblr:')) return 'tumblr';
    if (u.startsWith('arena:')) return 'arena';
    if (u.startsWith('http://') || u.startsWith('https://')) return 'rss';
    return '?';
  }

  function defaultInterval(): number {
    return Number(config.settings.default_poll_interval_minutes) || 30;
  }

  function feedToDraft(feed: FeedsConfigFeed): FeedDraft {
    return {
      url: feed.url,
      title: feed.title ?? '',
      category: feed.category ?? '',
      poll_interval_minutes: feed.poll_interval_minutes ? String(feed.poll_interval_minutes) : '',
    };
  }

  function draftToFeed(draft: FeedDraft): FeedsConfigFeed | string {
    const url = draft.url.trim();
    if (!url) return 'URL is required';
    const out: FeedsConfigFeed = { url };
    if (draft.title.trim()) out.title = draft.title.trim();
    if (draft.category.trim()) out.category = draft.category.trim();
    if (draft.poll_interval_minutes.trim()) {
      const n = Number(draft.poll_interval_minutes);
      if (!Number.isInteger(n) || n <= 0) {
        return 'Poll interval must be a positive integer';
      }
      out.poll_interval_minutes = n;
    }
    return out;
  }

  function startAdd() {
    modalError = '';
    adding = { url: '', title: '', category: '', poll_interval_minutes: '' };
  }

  function startEdit(idx: number) {
    modalError = '';
    editing = { idx, draft: feedToDraft(config.feeds[idx]) };
  }

  function cancelModal() {
    editing = null;
    adding = null;
    modalError = '';
  }

  function saveAdd() {
    if (!adding) return;
    const result = draftToFeed(adding);
    if (typeof result === 'string') {
      modalError = result;
      return;
    }
    // Reject duplicate URLs.
    if (config.feeds.some((f) => f.url === result.url)) {
      modalError = 'A subscription with that URL already exists.';
      return;
    }
    config.feeds = [...config.feeds, result];
    adding = null;
  }

  function saveEdit() {
    if (!editing) return;
    const result = draftToFeed(editing.draft);
    if (typeof result === 'string') {
      modalError = result;
      return;
    }
    const dup = config.feeds.findIndex((f, i) => i !== editing!.idx && f.url === result.url);
    if (dup >= 0) {
      modalError = 'A different subscription already uses that URL.';
      return;
    }
    config.feeds = config.feeds.map((f, i) => (i === editing!.idx ? result : f));
    editing = null;
  }

  function deleteFeed(idx: number) {
    const feed = config.feeds[idx];
    confirmDelete = {
      kind: 'feed',
      idx,
      label: feed.title || feed.url,
    };
  }

  function deleteCategory(idx: number) {
    const cat = config.categories[idx];
    confirmDelete = {
      kind: 'category',
      idx,
      label: cat.title || cat.slug,
    };
  }

  function performDelete() {
    if (!confirmDelete) return;
    if (confirmDelete.kind === 'feed') {
      config.feeds = config.feeds.filter((_, i) => i !== confirmDelete!.idx);
    } else {
      const slug = config.categories[confirmDelete.idx]?.slug;
      config.categories = config.categories.filter((_, i) => i !== confirmDelete!.idx);
      // Detach feeds from the deleted category (don't drop the feeds).
      if (slug) {
        config.feeds = config.feeds.map((f) =>
          f.category === slug ? { ...f, category: undefined } : f,
        );
      }
    }
    confirmDelete = null;
  }

  function addCategory() {
    const slug = prompt('New category slug (e.g. "blogs"):');
    if (!slug) return;
    const trimmed = slug.trim();
    if (!trimmed) return;
    if (config.categories.some((c) => c.slug === trimmed)) {
      error = `Category "${trimmed}" already exists.`;
      return;
    }
    config.categories = [...config.categories, { slug: trimmed, title: trimmed }];
  }

  function moveCategory(idx: number, delta: number) {
    const target = idx + delta;
    if (target < 0 || target >= config.categories.length) return;
    const next = config.categories.slice();
    [next[idx], next[target]] = [next[target], next[idx]];
    config.categories = next;
  }

  async function refreshNow() {
    if (refreshing) return;
    refreshing = true;
    error = '';
    info = '';
    try {
      const result = await refreshFeeds();
      info = `Queued ${result.feeds_queued} feed${result.feeds_queued === 1 ? '' : 's'} for an immediate poll. Reload the page in a moment to see updated diagnostics.`;
    } catch (e) {
      error = e instanceof Error ? e.message : 'Refresh failed';
    } finally {
      refreshing = false;
    }
  }

  async function onImport(e: Event) {
    const input = e.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    input.value = '';
    if (!file) return;
    importing = true;
    error = '';
    info = '';
    try {
      const result = await importOpml(file);
      info = `Imported: ${result.feeds_added} feeds added, ${result.feeds_updated} updated, ${result.categories_added} new categories, ${result.rewritten_bridger_urls} bridger URLs rewritten.`;
      await refresh();
    } catch (e) {
      error = e instanceof Error ? e.message : 'OPML import failed';
    } finally {
      importing = false;
    }
  }

  function categoryOptions(): string[] {
    return config.categories.map((c) => c.slug);
  }

  function formatDate(iso: string | null): string {
    if (!iso) return '—';
    try {
      return new Date(iso).toLocaleString();
    } catch {
      return iso;
    }
  }
</script>

<SettingsLayout
  title="Feed settings"
  description="Edit subscriptions, categories, and poll defaults. Saved straight to your local SQLite."
  {loading}
  {error}
  {info}
>
  {#snippet headerActions()}
    {#if dirty}
      <span class="dirty-badge">Unsaved changes</span>
    {/if}
    <Button variant="primary" onclick={save} disabled={!dirty || saving}>
      {saving ? 'Saving…' : 'Save changes'}
    </Button>
  {/snippet}

  {#if !moduleEnabled}
    <div class="banner info">
      Feeds module is disabled. Enable it in
      <a href="{base}/settings">Settings → Preferences</a> to manage subscriptions.
    </div>
  {:else}
    {#if diagnostics}
      <SettingsCard title="Diagnostics">
        {#snippet actions()}
          <Button variant="pill" size="sm" onclick={refreshNow} disabled={refreshing}>
            {refreshing ? 'Queueing…' : 'Refresh all now'}
          </Button>
        {/snippet}
        <div class="diag-grid card-grid">
          <div class="diag">
            <span class="diag-value">{diagnostics.total_feeds}</span>
            <span class="diag-label">subscriptions</span>
          </div>
          <div class="diag">
            <span class="diag-value">{diagnostics.total_entries}</span>
            <span class="diag-label">entries stored</span>
          </div>
          <div class="diag">
            <span class="diag-value">{diagnostics.unread_entries}</span>
            <span class="diag-label">unread</span>
          </div>
          <div class="diag" class:bad={diagnostics.error_feeds > 0}>
            <span class="diag-value">{diagnostics.error_feeds}</span>
            <span class="diag-label">in error</span>
          </div>
          <div class="diag wide">
            <span class="diag-value">{formatDate(diagnostics.last_poll_at)}</span>
            <span class="diag-label">last poll</span>
          </div>
        </div>
      </SettingsCard>
    {/if}

    <SettingsCard title="Settings">
      <SettingsField label="Default poll interval (minutes)">
        <input
          type="number"
          min="1"
          value={config.settings.default_poll_interval_minutes ?? ''}
          placeholder="30"
          oninput={(e) => {
            const v = (e.currentTarget as HTMLInputElement).value;
            const next = { ...config.settings };
            if (v === '') {
              delete next.default_poll_interval_minutes;
            } else {
              const n = Number(v);
              if (Number.isInteger(n) && n > 0) {
                next.default_poll_interval_minutes = n;
              }
            }
            config.settings = next;
          }}
        />
      </SettingsField>
    </SettingsCard>

    <SettingsCard title="Categories ({config.categories.length})">
      {#snippet actions()}
        <Button variant="pill" size="sm" onclick={addCategory}>+ Add category</Button>
      {/snippet}
      {#if config.categories.length === 0}
        <p class="empty">No categories yet. Categories group feeds in the sidebar.</p>
      {:else}
        <div class="table-scroll">
          <table class="grid">
            <thead>
              <tr>
                <th class="col-slug">Slug</th>
                <th class="col-title">Title</th>
                <th class="num col-count">Feeds</th>
                <th class="actions">Order</th>
                <th class="actions"></th>
              </tr>
            </thead>
            <tbody>
              {#each config.categories as cat, idx (cat.slug)}
                {@const count = config.feeds.filter((f) => f.category === cat.slug).length}
                <tr>
                  <td class="col-slug"><code>{cat.slug}</code></td>
                  <td class="col-title">
                    <input
                      type="text"
                      value={cat.title ?? ''}
                      placeholder={cat.slug}
                      oninput={(e) => {
                        config.categories = config.categories.map((c, i) =>
                          i === idx
                            ? { ...c, title: (e.currentTarget as HTMLInputElement).value }
                            : c,
                        );
                      }}
                    />
                  </td>
                  <td class="num col-count">{count}</td>
                  <td class="actions">
                    <button
                      class="icon-btn"
                      title="Move up"
                      onclick={() => moveCategory(idx, -1)}
                      disabled={idx === 0}
                      type="button">↑</button
                    >
                    <button
                      class="icon-btn"
                      title="Move down"
                      onclick={() => moveCategory(idx, 1)}
                      disabled={idx === config.categories.length - 1}
                      type="button">↓</button
                    >
                  </td>
                  <td class="actions">
                    <button
                      class="icon-btn danger"
                      title="Delete category"
                      onclick={() => deleteCategory(idx)}
                      type="button">×</button
                    >
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </SettingsCard>

    <SettingsCard title="Subscriptions ({config.feeds.length})">
      {#snippet actions()}
        <Button variant="pill" size="sm" onclick={startAdd}>+ Add subscription</Button>
      {/snippet}
      {#if config.feeds.length === 0}
        <p class="empty">No subscriptions yet. Add one above, or import an OPML file.</p>
      {:else}
        <div class="table-scroll">
          <table class="grid">
            <thead>
              <tr>
                <th class="type">Type</th>
                <th class="col-title">Subscription</th>
                <th class="col-category">Category</th>
                <th class="num col-interval">Interval</th>
                <th class="col-state">Last fetch</th>
                <th class="actions"></th>
              </tr>
            </thead>
            <tbody>
              {#each config.feeds as feed, idx (feed.url + idx)}
                {@const state = feedStateByUrl[feed.url]}
                <tr class:has-error={state && state.error_count > 0}>
                  <td class="type"><span class="type-pill">{detectSourceType(feed.url)}</span></td>
                  <td class="col-title">
                    <div class="title-cell">
                      <span class="title-text">{feed.title || feed.url}</span>
                      {#if feed.title && feed.title !== feed.url}
                        <code class="url-inline">{feed.url}</code>
                      {/if}
                    </div>
                  </td>
                  <td class="col-category">{feed.category || ''}</td>
                  <td class="num col-interval"
                    >{feed.poll_interval_minutes ?? defaultInterval()}m</td
                  >
                  <td class="col-state">
                    {#if state}
                      <div class="state">
                        <span>{formatDate(state.last_fetched_at)}</span>
                        {#if state.last_error}
                          <span class="state-error" title={state.last_error}>
                            {state.error_count}× err: {state.last_error.slice(0, 60)}
                          </span>
                        {/if}
                      </div>
                    {:else}
                      <span class="muted">never</span>
                    {/if}
                  </td>
                  <td class="actions">
                    <button
                      class="icon-btn"
                      title="Edit"
                      onclick={() => startEdit(idx)}
                      type="button">✎</button
                    >
                    <button
                      class="icon-btn danger"
                      title="Delete"
                      onclick={() => deleteFeed(idx)}
                      type="button">×</button
                    >
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </SettingsCard>

    <SettingsCard title="OPML">
      <p class="hint">
        Import subscriptions from an OPML export, or download the current list as OPML. Legacy
        bridger URLs (<code>localhost:8900/tumblr/…</code>,
        <code>localhost:8900/arena/…</code>) are rewritten to <code>tumblr:</code>
        / <code>arena:</code> on import.
      </p>
      <div class="opml-actions">
        <Button onclick={() => fileInput?.click()} disabled={importing}>
          {importing ? 'Importing…' : 'Import OPML…'}
        </Button>
        <a class="opml-link" href={exportOpmlUrl()} download="feeds.opml"> Download OPML </a>
      </div>
      <input
        bind:this={fileInput}
        type="file"
        accept=".opml,.xml,text/x-opml,application/xml,text/xml"
        onchange={onImport}
        hidden
      />
    </SettingsCard>

    {#each moduleServices as svc (svc.service)}
      <ServiceCard service={svc} onChanged={reloadServices} />
    {/each}
  {/if}
</SettingsLayout>

{#if adding}
  <Modal
    open={true}
    title="Add subscription"
    onOpenChange={(o) => {
      if (!o) cancelModal();
    }}
  >
    <div class="modal-body">
      <SettingsField label="URL">
        <input
          type="text"
          placeholder="https://example.com/feed.xml | tumblr:user | arena:slug"
          bind:value={adding.url}
        />
      </SettingsField>
      <SettingsField label="Title (optional)">
        <input type="text" bind:value={adding.title} />
      </SettingsField>
      <SettingsField label="Category">
        <Select
          value={adding.category}
          options={categorySelectOptions}
          onValueChange={(v) => {
            if (adding) adding.category = v;
          }}
          ariaLabel="Category"
          fullWidth
        />
      </SettingsField>
      <SettingsField label="Poll interval (minutes, optional)">
        <input
          type="number"
          min="1"
          placeholder={String(defaultInterval())}
          bind:value={adding.poll_interval_minutes}
        />
      </SettingsField>
      {#if modalError}
        <div class="banner error">{modalError}</div>
      {/if}
    </div>
    {#snippet footer()}
      <Button variant="ghost" onclick={cancelModal}>Cancel</Button>
      <Button variant="primary" onclick={saveAdd}>Add</Button>
    {/snippet}
  </Modal>
{/if}

{#if editing}
  <Modal
    open={true}
    title="Edit subscription"
    onOpenChange={(o) => {
      if (!o) cancelModal();
    }}
  >
    <div class="modal-body">
      <SettingsField label="URL">
        <input type="text" bind:value={editing.draft.url} />
      </SettingsField>
      <SettingsField label="Title">
        <input type="text" bind:value={editing.draft.title} />
      </SettingsField>
      <SettingsField label="Category">
        <Select
          value={editing.draft.category}
          options={categorySelectOptions}
          onValueChange={(v) => {
            if (editing) editing.draft.category = v;
          }}
          ariaLabel="Category"
          fullWidth
        />
      </SettingsField>
      <SettingsField label="Poll interval (minutes)">
        <input
          type="number"
          min="1"
          placeholder={String(defaultInterval())}
          bind:value={editing.draft.poll_interval_minutes}
        />
      </SettingsField>
      {#if modalError}
        <div class="banner error">{modalError}</div>
      {/if}
    </div>
    {#snippet footer()}
      <Button variant="ghost" onclick={cancelModal}>Cancel</Button>
      <Button variant="primary" onclick={saveEdit}>Save</Button>
    {/snippet}
  </Modal>
{/if}

{#if confirmDelete}
  <ConfirmDialog
    open={true}
    title={confirmDelete.kind === 'category' ? 'Delete category' : 'Delete feed'}
    confirmLabel="Delete"
    onConfirm={performDelete}
    onCancel={() => (confirmDelete = null)}
  >
    {#snippet body()}
      <p>
        Are you sure you want to remove the {confirmDelete?.kind}
        <strong>{confirmDelete?.label}</strong>?
        {#if confirmDelete?.kind === 'category'}
          Subscriptions in this category will keep their entries but become uncategorised.
        {/if}
        Changes apply when you save.
      </p>
    {/snippet}
  </ConfirmDialog>
{/if}

<style>
  /* Shared .settings/.card/.field/.grid/.banner/.icon-btn primitives live in
	   web/src/lib/styles/settings.css (imported by app.css). Only feeds-specific
	   styling (diagnostics, type pill, title cell, container queries) stays. */

  .diag-grid {
    --card-min: 140px;
    --card-gap: 0.5rem;
  }

  .diag {
    background: var(--surface-raised);
    border-radius: var(--radius-card);
    padding: 0.5rem 0.75rem;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }

  .diag.wide {
    grid-column: span 2;
  }

  .diag.bad .diag-value {
    color: #e88;
  }

  .diag-value {
    font-size: var(--text-base);
    font-weight: 600;
    color: var(--text-primary);
  }

  .diag-label {
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .col-title {
    width: auto;
  }

  .col-slug {
    width: 12rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .col-count {
    width: 4rem;
  }

  .title-cell {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    min-width: 0;
  }

  .title-text {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-primary);
  }

  .col-category {
    width: 7rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .col-interval {
    width: 4.5rem;
  }

  .col-state {
    width: 11rem;
  }

  .url-inline {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    font-size: var(--text-xs);
    color: var(--text-dim);
    background: transparent;
    padding: 0;
    overflow-wrap: anywhere;
    word-break: break-all;
    line-height: 1.3;
  }

  .grid tbody tr.has-error {
    background: rgba(204, 102, 102, 0.05);
  }

  .grid td.type,
  .grid th.type {
    width: 4.5rem;
  }

  .grid td.num,
  .grid th.num {
    text-align: right;
    white-space: nowrap;
  }

  /* Override shared .grid actions width — feeds row has two icon buttons. */
  .grid td.actions,
  .grid th.actions {
    width: 4.5rem;
  }

  .grid input[type='text'] {
    width: 100%;
    background: transparent;
    color: var(--text-primary);
    border: 1px solid transparent;
    border-radius: 0.2rem;
    padding: 0.2rem 0.3rem;
    font: inherit;
    font-size: var(--text-sm);
  }

  .grid input[type='text']:focus,
  .grid input[type='text']:hover {
    border-color: var(--border-default);
    background: var(--surface-base);
    outline: none;
  }

  .type-pill {
    display: inline-block;
    font-size: var(--text-xs);
    padding: 0.05rem 0.4rem;
    border-radius: var(--radius-pill);
    background: var(--surface-raised);
    color: var(--text-muted);
  }

  .state {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    font-size: var(--text-xs);
    min-width: 0;
    overflow: hidden;
  }

  .state > span:first-child {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .state-error {
    color: #e88;
    max-width: 32ch;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .opml-actions {
    display: flex;
    gap: 0.6rem;
    align-items: center;
    flex-wrap: wrap;
  }

  .opml-link {
    font-size: var(--text-sm);
    color: var(--text-muted);
    text-decoration: underline;
  }

  .opml-link:hover {
    color: var(--text-primary);
  }

  .modal-body {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }

  /* Container-based: hide low-priority table columns as the content area
	   narrows. Uses container queries because the page sits inside a shell
	   with a 220px sidebar — viewport-based @media queries would misjudge
	   the actual content width. */
  @container settings (max-width: 880px) {
    .col-state {
      display: none;
    }
  }

  @container settings (max-width: 520px) {
    .col-category,
    .col-interval,
    .col-count {
      display: none;
    }

    .grid td.type,
    .grid th.type {
      width: 3rem;
    }

    .grid td.actions,
    .grid th.actions {
      width: auto;
    }

    .icon-btn {
      padding: 0.25rem 0.4rem;
    }
  }

  @container settings (max-width: 420px) {
    .diag-grid {
      --card-min: 110px;
    }

    .diag.wide {
      grid-column: span 1;
    }

    .diag {
      padding: 0.4rem 0.5rem;
    }
  }

  /* Light theme overrides — dark rules above untouched. */
  :global(:root[data-theme='light']) .diag.bad .diag-value {
    color: #c0271d;
  }
  :global(:root[data-theme='light']) .state-error {
    color: #c0271d;
  }
</style>
