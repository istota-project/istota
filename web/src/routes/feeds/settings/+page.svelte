<script lang="ts">
  import { ArrowDown, ArrowUp } from '@lucide/svelte';
  import { createConfirm } from '$lib/ui/confirm.svelte';
  import { invalidateNavUnreadCounts } from '$lib/stores/navUnread';
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
    type FeedsConfigSettings,
    type FeedsConfigFeed,
    type FeedsConfigCategory,
    type FeedsDiagnostics,
    type FeedsFeedState,
    type ServiceCard as ServiceCardData,
  } from '$lib/api';
  import {
    Alert,
    Button,
    ConfirmDialog,
    EmptyState,
    IconButton,
    FormModal,
    KebabMenu,
    type KebabItem,
    type SelectOption,
    Card,
    Field,
    Input,
    Badge,
    Timestamp,
  } from '$lib/components/ui';
  import { ServiceCard, SettingsLayout } from '$lib/components/settings';
  import FeedSubscriptionModal, {
    type FeedDraft,
  } from '$lib/components/FeedSubscriptionModal.svelte';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';
  import { createDraft } from '$lib/ui/draft.svelte';
  import { parseOptionalInt } from '$lib/ui/parse';

  let loading = $state(true);
  let saving = $state(false);
  let importing = $state(false);
  let refreshing = $state(false);
  let error = $state('');
  let info = $state('');
  // Until the config has loaded once there is nothing to edit: a failure then
  // takes the pane, or the one save would send the empty draft.
  let loaded = $state(false);

  // The loaded config is the draft the page edits, and the one save sends.
  const draft = createDraft<FeedsConfigPayload>({ settings: {}, categories: [], feeds: [] });
  const config = $derived(draft.value);
  let diagnostics: FeedsDiagnostics | null = $state(null);
  let feedStateByUrl: Record<string, FeedsFeedState> = $state({});
  let moduleServices: ServiceCardData[] = $state([]);
  let moduleEnabled = $state(true);

  let editing: { idx: number; draft: FeedDraft } | null = $state(null);
  let adding: FeedDraft | null = $state(null);
  // A category is two fields, so it gets a modal like a subscription rather
  // than the browser prompt it used to open — which could ask one question,
  // and reported a duplicate slug only after closing and dropping the draft.
  let addingCategory: { slug: string; title: string } | null = $state(null);
  const confirmDelete = createConfirm<{ kind: 'feed' | 'category'; idx: number; label: string }>();

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
        loaded = true;
        return;
      }
      const data = await getFeedsConfig();
      draft.reset(data.config);
      loaded = true;
      diagnostics = data.diagnostics;
      feedStateByUrl = Object.fromEntries(data.feed_state.map((s) => [s.url, s]));
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
      invalidateNavUnreadCounts();
      info = `Saved (${result.sync.feeds_added} added, ${result.sync.feeds_updated} updated, ${result.sync.categories_added} new categories).`;
      draft.reset();
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

  useSettingsSave(() => (moduleEnabled ? { dirty: draft.dirty, saving, save } : null));

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
    const interval = parseOptionalInt(draft.poll_interval_minutes, { min: 1 });
    if (interval === undefined) return 'Poll interval must be a positive integer';
    if (interval !== null) out.poll_interval_minutes = interval;
    return out;
  }

  // A blank field unsets the setting; text that is not a whole number at or
  // above `min` leaves it as it was.
  function setOptionalSetting(key: keyof FeedsConfigSettings, text: string, min: number) {
    const value = parseOptionalInt(text, { min });
    if (value === undefined) return;
    const next = { ...config.settings };
    if (value === null) delete next[key];
    else next[key] = value;
    config.settings = next;
  }

  function startAdd() {
    adding = { url: '', title: '', category: '', poll_interval_minutes: '' };
  }

  function startEdit(idx: number) {
    editing = { idx, draft: feedToDraft(config.feeds[idx]) };
  }

  function cancelModal() {
    editing = null;
    adding = null;
    addingCategory = null;
  }

  // Applies the subscription modal's draft to the page's config, or throws
  // the reason it cannot, which the modal shows.
  function saveFeed(draft: FeedDraft) {
    const result = draftToFeed(draft);
    if (typeof result === 'string') throw new Error(result);
    if (editing) {
      const idx = editing.idx;
      if (config.feeds.some((f, i) => i !== idx && f.url === result.url)) {
        throw new Error('A different subscription already uses that URL.');
      }
      config.feeds = config.feeds.map((f, i) => (i === idx ? result : f));
      editing = null;
    } else {
      if (config.feeds.some((f) => f.url === result.url)) {
        throw new Error('A subscription with that URL already exists.');
      }
      config.feeds = [...config.feeds, result];
      adding = null;
    }
  }

  function deleteFeed(idx: number) {
    const feed = config.feeds[idx];
    confirmDelete.ask({
      kind: 'feed',
      idx,
      label: feed.title || feed.url,
    });
  }

  function deleteCategory(idx: number) {
    const cat = config.categories[idx];
    confirmDelete.ask({
      kind: 'category',
      idx,
      label: cat.title || cat.slug,
    });
  }

  // No Duplicate here, unlike briefings: a feed is identified by its URL and a
  // category by its slug, both of which must stay unique — a copy would only be
  // valid once its identity had been retyped, which is the Add form.
  function feedMenu(idx: number): KebabItem[] {
    return [
      { label: 'Edit', onSelect: () => startEdit(idx) },
      { label: 'Delete', danger: true, onSelect: () => deleteFeed(idx) },
    ];
  }

  function categoryMenu(idx: number): KebabItem[] {
    return [{ label: 'Delete', danger: true, onSelect: () => deleteCategory(idx) }];
  }

  function performDelete() {
    const target = confirmDelete.target;
    if (!target) return;
    if (target.kind === 'feed') {
      config.feeds = config.feeds.filter((_, i) => i !== target.idx);
    } else {
      const slug = config.categories[target.idx]?.slug;
      config.categories = config.categories.filter((_, i) => i !== target.idx);
      // Detach feeds from the deleted category (don't drop the feeds).
      if (slug) {
        config.feeds = config.feeds.map((f) =>
          f.category === slug ? { ...f, category: undefined } : f,
        );
      }
    }
    confirmDelete.cancel();
  }

  function startAddCategory() {
    addingCategory = { slug: '', title: '' };
  }

  function saveAddCategory() {
    if (!addingCategory) return;
    const slug = addingCategory.slug.trim();
    if (!slug) throw new Error('Slug is required');
    if (config.categories.some((c) => c.slug === slug)) {
      throw new Error(`Category "${slug}" already exists.`);
    }
    // A blank title still mirrors the slug, and that is the server's rule
    // rather than a leftover from the prompt: `feed_categories.title` is NOT
    // NULL, and the reader's sidebar reads a falsy title as "uncategorized"
    // (`routes/feeds/+layout.svelte`), so a genuinely blank title would file a
    // real category under the uncategorized heading. Mirroring here rather
    // than letting `_apply_config_to_db` do it keeps the table showing what a
    // reload will show. What the modal changes is that you can now set the
    // title *while* creating the category, instead of creating it under the
    // slug and correcting it afterwards.
    const title = addingCategory.title.trim();
    config.categories = [...config.categories, { slug, title: title || slug }];
    addingCategory = null;
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
</script>

<SettingsLayout
  title="Feed settings"
  description="Edit subscriptions, categories, and poll defaults. Saved straight to your local SQLite."
  {loading}
  error={loaded ? error : ''}
  loadError={loaded ? '' : error}
  onRetry={refresh}
  {info}
>
  {#if !moduleEnabled}
    <Alert appearance="inline" variant="info">
      Feeds module is disabled. Enable it in
      <a href="{base}/settings">Settings → Preferences</a> to manage subscriptions.
    </Alert>
  {:else}
    {#if diagnostics}
      <Card title="Diagnostics">
        {#snippet actions()}
          <Button
            variant="pill"
            size="sm"
            onclick={refreshNow}
            loading={refreshing}
            loadingLabel="Queueing…"
          >
            Refresh all now
          </Button>
        {/snippet}
        <div class="diag-grid card-grid">
          <div class="diag">
            <span class="diag-value">{diagnostics.total_feeds}</span>
            <span class="diag-label caption">subscriptions</span>
          </div>
          <div class="diag">
            <span class="diag-value">{diagnostics.total_entries}</span>
            <span class="diag-label caption">entries stored</span>
          </div>
          <div class="diag">
            <span class="diag-value">{diagnostics.unread_entries}</span>
            <span class="diag-label caption">unread</span>
          </div>
          <div class="diag" class:bad={diagnostics.error_feeds > 0}>
            <span class="diag-value">{diagnostics.error_feeds}</span>
            <span class="diag-label caption">in error</span>
          </div>
          <div class="diag wide">
            <Timestamp
              class="diag-value"
              value={diagnostics.last_poll_at}
              format="datetime"
              empty="—"
            />
            <span class="diag-label caption">last poll</span>
          </div>
        </div>
      </Card>
    {/if}

    <Card title="Settings">
      <Field label="Default poll interval (minutes)">
        <Input
          type="number"
          min="1"
          value={config.settings.default_poll_interval_minutes ?? ''}
          placeholder="30"
          oninput={(e) =>
            setOptionalSetting(
              'default_poll_interval_minutes',
              (e.currentTarget as HTMLInputElement).value,
              1,
            )}
        />
      </Field>
      <Field
        label="Hide repeated images seen in the last (days)"
        hint="A reblog of a picture you scrolled past recently shows the post but not the image again. 0 turns it off; blank uses the default (14)."
      >
        <Input
          type="number"
          min="0"
          value={config.settings.image_dedupe_window_days ?? ''}
          placeholder="14"
          oninput={(e) =>
            setOptionalSetting(
              'image_dedupe_window_days',
              (e.currentTarget as HTMLInputElement).value,
              0,
            )}
        />
      </Field>
      <Field
        label="Keep read entries for (days)"
        hint="Counted from when an entry was added to your reader, not from when it was published. Unread and starred entries are always kept, and so is anything the feed still returns. At least 50 entries per feed are kept whatever their age, or the maximum below if you set it lower. 0 turns age pruning off; blank uses the default (90). Older entries disappear from the reader."
      >
        <Input
          type="number"
          min="0"
          max="365000"
          value={config.settings.entry_retention_days ?? ''}
          placeholder="90"
          oninput={(e) =>
            setOptionalSetting(
              'entry_retention_days',
              (e.currentTarget as HTMLInputElement).value,
              0,
            )}
        />
      </Field>
      <Field
        label="Maximum stored entries per feed"
        hint="The most recently added entries are kept. Starred entries are always kept and can put a feed above this limit. 0 turns the limit off; blank uses the default (5000). Raising it lets the next full fetch store more."
      >
        <Input
          type="number"
          min="0"
          max="365000"
          value={config.settings.max_entries_per_feed ?? ''}
          placeholder="5000"
          oninput={(e) =>
            setOptionalSetting(
              'max_entries_per_feed',
              (e.currentTarget as HTMLInputElement).value,
              0,
            )}
        />
      </Field>
    </Card>

    <Card title="Categories ({config.categories.length})">
      {#snippet actions()}
        <Button variant="pill" size="sm" onclick={startAddCategory}>+ Add category</Button>
      {/snippet}
      {#if config.categories.length === 0}
        <EmptyState>No categories yet. Categories group feeds in the sidebar.</EmptyState>
      {:else}
        <div class="table-scroll">
          <table class="table table--fixed">
            <thead>
              <tr>
                <th class="col-slug">Slug</th>
                <th class="col-title">Title</th>
                <th class="num col-count">Feeds</th>
                <th class="col-actions col-order">Order</th>
                <th class="col-actions"></th>
              </tr>
            </thead>
            <tbody>
              {#each config.categories as cat, idx (cat.slug)}
                {@const count = config.feeds.filter((f) => f.category === cat.slug).length}
                <tr>
                  <td class="col-slug"><code>{cat.slug}</code></td>
                  <td class="col-title">
                    <Input
                      type="text"
                      variant="ghost"
                      aria-label="Title for {cat.slug}"
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
                  <td class="col-actions col-order">
                    <IconButton
                      size="sm"
                      label="Move up"
                      title="Move up"
                      onclick={() => moveCategory(idx, -1)}
                      disabled={idx === 0}
                      icon={ArrowUp}
                    />
                    <IconButton
                      size="sm"
                      label="Move down"
                      title="Move down"
                      onclick={() => moveCategory(idx, 1)}
                      disabled={idx === config.categories.length - 1}
                      icon={ArrowDown}
                    />
                  </td>
                  <td class="col-actions">
                    <KebabMenu items={categoryMenu(idx)} ariaLabel="Category actions" />
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </Card>

    <Card title="Subscriptions ({config.feeds.length})">
      {#snippet actions()}
        <Button variant="pill" size="sm" onclick={startAdd}>+ Add subscription</Button>
      {/snippet}
      {#if config.feeds.length === 0}
        <EmptyState>No subscriptions yet. Add one above, or import an OPML file.</EmptyState>
      {:else}
        <div class="table-scroll">
          <table class="table table--fixed">
            <thead>
              <tr>
                <th class="type">Type</th>
                <th class="col-title">Subscription</th>
                <th class="col-category">Category</th>
                <th class="num col-interval">Interval</th>
                <th class="col-state">Last fetch</th>
                <th class="col-actions"></th>
              </tr>
            </thead>
            <tbody>
              {#each config.feeds as feed, idx (feed.url + idx)}
                {@const state = feedStateByUrl[feed.url]}
                <tr class:is-danger={state && state.error_count > 0}>
                  <td class="type"
                    ><Badge tone="subtle" uppercase={false}>{detectSourceType(feed.url)}</Badge></td
                  >
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
                        <Timestamp
                          class="truncate"
                          value={state.last_fetched_at}
                          format="datetime"
                          empty="—"
                        />
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
                  <td class="col-actions">
                    <KebabMenu items={feedMenu(idx)} ariaLabel="Subscription actions" />
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </Card>

    <Card title="OPML">
      <p class="description">
        Import subscriptions from an OPML export, or download the current list as OPML. Legacy
        bridger URLs (<code>localhost:8900/tumblr/…</code>,
        <code>localhost:8900/arena/…</code>) are rewritten to <code>tumblr:</code>
        / <code>arena:</code> on import.
      </p>
      <div class="hstack wrap">
        <Button onclick={() => fileInput?.click()} loading={importing} loadingLabel="Importing…">
          Import OPML…
        </Button>
        <a class="opml-link link-muted" href={exportOpmlUrl()} download="feeds.opml">
          Download OPML
        </a>
      </div>
      <input
        bind:this={fileInput}
        type="file"
        accept=".opml,.xml,text/x-opml,application/xml,text/xml"
        onchange={onImport}
        hidden
      />
    </Card>

    {#each moduleServices as svc (svc.service)}
      <ServiceCard service={svc} onChanged={reloadServices} />
    {/each}
  {/if}
</SettingsLayout>

{#if adding || editing}
  <FeedSubscriptionModal
    mode={editing ? 'edit' : 'add'}
    initial={editing ? editing.draft : adding!}
    categoryOptions={categorySelectOptions}
    defaultInterval={defaultInterval()}
    onSubmit={saveFeed}
    onCancel={cancelModal}
  />
{/if}

{#if addingCategory}
  <FormModal
    open={true}
    title="Add category"
    submitLabel="Add"
    onSubmit={saveAddCategory}
    onCancel={cancelModal}
  >
    <div class="vstack" style:--vstack-gap="var(--space-2)">
      <Field label="Slug">
        <Input type="text" placeholder="blogs" bind:value={addingCategory.slug} />
      </Field>
      <Field label="Title (optional)">
        <Input type="text" placeholder={addingCategory.slug} bind:value={addingCategory.title} />
      </Field>
    </div>
  </FormModal>
{/if}

{#if confirmDelete.target}
  <ConfirmDialog
    bind:open={confirmDelete.open}
    title={confirmDelete.target.kind === 'category' ? 'Delete category' : 'Delete feed'}
    confirmLabel="Delete"
    onConfirm={performDelete}
  >
    {#snippet body()}
      <p>
        Are you sure you want to remove the {confirmDelete.target?.kind}
        <strong>{confirmDelete.target?.label}</strong>?
        {#if confirmDelete.target?.kind === 'category'}
          Subscriptions in this category will keep their entries but become uncategorised.
        {/if}
        Changes apply when you save.
      </p>
    {/snippet}
  </ConfirmDialog>
{/if}

<style>
  /* Shared .settings/.card/.field/.banner/.icon-btn primitives live in
	   web/src/lib/styles/settings.css (imported by app.css). Only feeds-specific
	   styling (diagnostics, type pill, title cell, container queries) stays. */

  .diag-grid {
    --card-min: 140px;
    --card-gap: 0.5rem;
  }

  .diag {
    background: var(--surface-raised);
    border-radius: var(--radius-card);
    padding: var(--space-2) var(--space-3);
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }

  .diag.wide {
    grid-column: span 2;
  }

  .diag.bad .diag-value {
    color: var(--status-danger-fg);
  }

  .diag :global(.diag-value) {
    font-size: var(--text-base);
    font-weight: var(--weight-semibold);
    color: var(--text-primary);
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
    line-height: var(--leading-tight);
  }

  .type {
    width: 4.5rem;
  }

  /* Row actions are a single kebab at the table's own width; only the reorder
	   column holds a pair of icon buttons, so it asks for more. */
  .col-order {
    --table-actions-width: 4.5rem;
  }

  .state {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    font-size: var(--text-xs);
    min-width: 0;
    overflow: hidden;
  }

  .state-error {
    color: var(--status-danger-fg);
    max-width: 32ch;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .opml-link {
    font-size: var(--text-sm);
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

    .type {
      width: 3rem;
    }

    /* Every column but the title gets a content-sized width, so .col-title
		   (width: auto) absorbs all remaining space. Leaving the action columns
		   on `auto` made them share the leftovers evenly with the title, which
		   is what squeezed both titles down to a few characters. */
    .col-slug {
      width: 7rem;
    }

    .col-order {
      --table-actions-width: 4rem;
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
      padding: var(--space-2) var(--space-2);
    }
  }
</style>
