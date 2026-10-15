<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    AuthError,
    getGroupBriefings,
    getGroupBriefing,
    getGroupBriefingRuns,
    getGroupBriefingRun,
    getGroupBriefingDelivery,
    retryGroupBriefingDelivery,
    runGroupBriefing,
    getGroupBriefingPreferences,
    saveGroupBriefingPreferences,
    getGroupDeliveryPreferences,
    saveGroupDeliveryPreferences,
    groupBriefingDownloadUrl,
    type GroupBriefingDefinition,
    type GroupBriefingRun,
    type BriefingDelivery,
    type BriefingDeliveryPreferences,
  } from '$lib/api';
  import { Button, EmptyState, PageLoading, Select } from '$lib/components/ui';
  import { SettingsCard } from '$lib/components/settings';
  import { createUrlSelection } from '$lib/navigation/urlSelection.svelte';
  import { formatDateTime } from '$lib/dateFormat';
  import BriefingEdition from './BriefingEdition.svelte';

  let {
    groupId,
    label,
    canCreate = false,
  }: { groupId: string; label: string; canCreate?: boolean } = $props();
  let definitions = $state<GroupBriefingDefinition[]>([]);
  let definition = $state<GroupBriefingDefinition | null>(null);
  let runs = $state<GroupBriefingRun[]>([]);
  let total = $state(0);
  let current = $state<GroupBriefingRun | null>(null);
  let delivery = $state<BriefingDelivery | null>(null);
  let preferences = $state<BriefingDeliveryPreferences | null>(null);
  let groupPreferences = $state<BriefingDeliveryPreferences | null>(null);
  let loading = $state(true);
  let busy = $state(false);
  let error = $state('');
  let notice = $state('');
  let acknowledge = $state(false);
  let denied = $state(false);
  let createAllowed = $state(false);
  let active = true;
  let request = 0;
  let selected = $state<{ name: string; run: number | null } | null>(null);
  const author = $derived(!denied && !!definition?.capabilities.author_briefings);
  const ready = $derived(current?.state === 'published' || current?.state === 'preview_ready');
  const editor = (name?: string) =>
    `${base}/briefings/settings?group=${encodeURIComponent(groupId)}&${name ? `briefing=${encodeURIComponent(name)}` : 'new=1'}`;
  const fmt = (value?: string | null) =>
    value ? formatDateTime(value, { dateStyle: 'medium', timeStyle: 'short' }) : 'None';
  const selection = createUrlSelection<{ name: string; run: number | null }>({
    key: 'groupBriefing',
    params: ['briefing', 'run'],
    encode: (s) => ({ briefing: s.name, ...(s.run ? { run: String(s.run) } : {}) }),
    decode: (p) => ({
      name: p.briefing ?? '',
      run: Number.isSafeInteger(Number(p.run)) && Number(p.run) > 0 ? Number(p.run) : null,
    }),
    read: () => selected,
    apply: (s) => open(s.name, s.run),
  });
  selection.start();

  function failure(e: unknown) {
    error = e instanceof Error ? e.message : 'Could not load group briefings.';
    const status = (e as { status?: number })?.status;
    if (e instanceof AuthError || [401, 403, 404].includes(status ?? 0)) {
      ++request;
      current = null;
      delivery = null;
      definition = null;
      preferences = null;
      groupPreferences = null;
      runs = [];
      definitions = [];
      denied = true;
    }
  }

  async function readRun(run: number, name: string, token: number) {
    const item = await getGroupBriefingRun(groupId, name, run);
    const status =
      item.state === 'published' ? await getGroupBriefingDelivery(groupId, name, run) : null;
    if (!active || token !== request) return;
    current = item;
    delivery = status;
    selected = { name, run };
  }

  async function open(name: string, run: number | null = null) {
    const token = ++request;
    current = null;
    delivery = null;
    definition = null;
    preferences = null;
    acknowledge = false;
    error = '';
    notice = '';
    loading = true;
    selected = { name, run };
    try {
      const [live, history, prefs] = await Promise.all([
        getGroupBriefing(groupId, name),
        getGroupBriefingRuns(groupId, name),
        getGroupBriefingPreferences(groupId, name),
      ]);
      if (!active || token !== request) return;
      definition = live;
      denied = false;
      runs = history.items;
      total = history.total;
      preferences = prefs;
      const id =
        run ?? history.items.find((item) => item.state === 'published')?.id ?? history.items[0]?.id;
      if (id) await readRun(id, name, token);
    } catch (e) {
      if (active && token === request) failure(e);
    } finally {
      if (active) loading = false;
    }
  }

  async function load() {
    const token = ++request;
    current = null;
    delivery = null;
    definition = null;
    loading = true;
    error = '';
    try {
      const [result, defaults] = await Promise.all([getGroupBriefings(groupId), getGroupDeliveryPreferences(groupId)]);
      if (!active || token !== request) return;
      definitions = result.items;
      groupPreferences = defaults;
      createAllowed =
        result.capabilities?.author_briefings ??
        result.items[0]?.capabilities.author_briefings ??
        canCreate;
      denied = false;
      const initial = selected ?? selection.current();
      const name = initial?.name || definitions[0]?.name;
      if (name) await open(name, initial?.run ?? null);
    } catch (e) {
      if (active && token === request) failure(e);
    } finally {
      if (active) loading = false;
    }
  }

  async function action(kind: 'publish' | 'preview' | 'retry' | 'preferences') {
    if (!definition || busy) return;
    const name = definition.name;
    const token = request;
    busy = true;
    error = '';
    try {
      if (kind === 'preferences' && preferences) {
        const result = await saveGroupBriefingPreferences(groupId, name, preferences);
        if (active && token === request) preferences = result;
      } else if (kind === 'retry' && current) {
        const result = await retryGroupBriefingDelivery(groupId, name, current.id, acknowledge);
        if (!active || token !== request) return;
        acknowledge = false;
        await open(name, current.id);
        notice = `${result.retried} deliveries queued from the saved edition. No new generation.`;
      } else if (kind === 'publish' || kind === 'preview') {
        const result = await runGroupBriefing(groupId, name, kind, crypto.randomUUID());
        if (!active || token !== request) return;
        selection.push({ name, run: result.id });
      }
    } catch (e) {
      if (active && token === request) failure(e);
    } finally {
      if (active) busy = false;
    }
  }

  async function saveGroupPreferences() {
    if (!groupPreferences || busy) return;
    const token = request;
    busy = true;
    error = '';
    try {
      const result = await saveGroupDeliveryPreferences(groupId, {
        outbound_muted: groupPreferences.outbound_muted,
        in_app_muted: groupPreferences.in_app_muted,
      });
      if (active && token === request) {
        groupPreferences = result;
        if (definition) {
          const next = await getGroupBriefingPreferences(groupId, definition.name);
          if (active && token === request) preferences = next;
        }
      }
    } catch (e) {
      if (active && token === request) failure(e);
    } finally {
      if (active) busy = false;
    }
  }

  async function older() {
    if (!definition) return;
    const token = request;
    try {
      const result = await getGroupBriefingRuns(groupId, definition.name, runs.length);
      if (active && token === request) {
        runs = [...runs, ...result.items];
        total = result.total;
      }
    } catch (e) {
      if (active && token === request) failure(e);
    }
  }

  onMount(() => {
    void load();
    // Live roles and membership govern an already-open reader too.
    const refresh = () => {
      if (!busy) void load();
    };
    const timer = setInterval(refresh, 30000);
    window.addEventListener('focus', refresh);
    return () => {
      active = false;
      ++request;
      clearInterval(timer);
      window.removeEventListener('focus', refresh);
    };
  });
</script>

<SettingsCard title="Briefings">
  {#snippet actions()}
    <Button size="sm" onclick={() => load()} disabled={loading || busy}>Refresh briefings</Button>
    {#if !denied && createAllowed}<Button size="sm" href={editor()}>New group briefing</Button>{/if}
  {/snippet}
  <p class="caption">{label} · Retained editions are shared with current and future members.</p>
  {#if error}<p role="alert" class="error">{error}</p>{/if}
  {#if notice}<p role="status">{notice}</p>{/if}
  {#if loading}<PageLoading />{:else if !definitions.length && !error}<EmptyState size="sm"
      >No group briefings yet.</EmptyState
    >{/if}
    {#if groupPreferences && !denied}
      <div class="preferences">
        <p class="caption">These defaults apply to current and future briefings in this group. Membership and history access stay available.</p>
        <label><input type="checkbox" bind:checked={groupPreferences.outbound_muted} /> Mute external delivery for all briefings in this group</label>
        <label><input type="checkbox" bind:checked={groupPreferences.in_app_muted} /> Mute notifications for all briefings in this group</label>
        <Button size="sm" onclick={saveGroupPreferences} disabled={busy}>Save group delivery preferences</Button>
      </div>
    {/if}
  {#if definitions.length}
    <div class="definition-list">
      {#each definitions as item (item.id)}
        <div class="definition-row">
          <Button size="sm" onclick={() => selection.push({ name: item.name, run: null })}
            >{item.title || item.name}</Button
          >
          <span class="caption"
            >{label} · {item.channel} · {item.enabled ? item.cron : 'Disabled'} · {item.timezone}</span
          >
          <span class="caption"
            >Next run: {fmt(item.next_run)} · Last run: {item.last_run?.state ?? 'None'}</span
          >
        </div>
      {/each}
    </div>
  {/if}
  {#if definition}
    <p class="audience">
      Generated once per run. The same briefing is sent to members of {label} via {definition.channel}.
    </p>
    <div class="actions">
      {#if author}
        <Button size="sm" href={editor(definition.name)}>Edit group briefing</Button>
        <Button
          size="sm"
          onclick={() => action('publish')}
          disabled={busy || !definition.capabilities.generate_briefings}>Generate now</Button
        >
        <Button
          size="sm"
          onclick={() => action('preview')}
          disabled={busy || !definition.capabilities.generate_briefings}>Preview</Button
        >
      {:else if definition.source_approvals?.length}
        <Button size="sm" href={editor(definition.name)}>Review sources</Button>
      {/if}
    </div>
    {#if author}<p class="caption">
        Generate now creates a new edition for members. Preview is private to current authors and
        sends nothing.
      </p>{/if}
    {#if preferences}
      <div class="preferences">
        <label
          ><input type="checkbox" bind:checked={preferences.outbound_muted} /> Mute my external delivery</label
        >
        <label
          ><input type="checkbox" bind:checked={preferences.in_app_muted} /> Mute my in-app notifications</label
        >
        <Button size="sm" onclick={() => action('preferences')} disabled={busy}
          >Save my delivery preferences</Button
        >
      </div>
    {/if}
    {#if runs.length}
      <Select
        value={String(selected?.run ?? '')}
        options={runs.map((run) => ({
          value: String(run.id),
          label: `${run.mode === 'preview' ? 'Preview' : 'Edition'} ${run.id}: ${run.subject || run.state}`,
        }))}
        ariaLabel="Group edition"
        onValueChange={(value) => selection.push({ name: definition!.name, run: Number(value) })}
      />
      {#if runs.length < total}<Button size="sm" onclick={older}>Load older group editions</Button
        >{/if}
    {/if}
    {#if current}
      <p>
        Generation: {current.state}{current.error_code ? ` (${current.error_code})` : ''} · {fmt(
          current.published_at || current.created_at,
        )}
      </p>
      {#if current.mode === 'preview'}<p class="audience">
          Private preview. Nothing has been sent. Generate now creates a fresh edition.
        </p>{/if}
      {#if delivery}
        <p>
          Delivery: {Object.entries(delivery.counts)
            .map(([state, count]) => `${count} ${state}`)
            .join(', ') || 'No recipients'}
        </p>
        <ul>
          {#each delivery.recipients as recipient}<li>
              {recipient.display_name} · {recipient.channel} · {recipient.state}{recipient.error_code
                ? ` (${recipient.error_code})`
                : ''}
            </li>{/each}
        </ul>
        {#if author && (delivery.counts.failed || delivery.counts.uncertain)}
          <p class="caption">Retry uses this saved edition and does not generate new content.</p>
          {#if delivery.counts.uncertain}<label
              ><input type="checkbox" bind:checked={acknowledge} /> I understand uncertain deliveries
              may be duplicated.</label
            >{/if}
          <Button
            size="sm"
            onclick={() => action('retry')}
            disabled={busy || (!!delivery.counts.uncertain && !acknowledge)}
            >Retry failed deliveries</Button
          >
        {/if}
      {/if}
      {#if ready}
        <Button
          size="sm"
          href={groupBriefingDownloadUrl(groupId, definition.name, current.id)}
          download>Download Markdown</Button
        >
        {#key `${groupId}:${current.id}`}<BriefingEdition
            edition={{
              id: current.id,
              briefing_name: definition.name,
              subject: current.subject ?? definition.title,
              body_md: current.body_md,
              generated_at: current.published_at || current.created_at || '',
              task_id: null,
              delivered_to: [],
            }}
          />{/key}
      {/if}
    {/if}
  {/if}
</SettingsCard>

<style>
  .definition-list,
  .preferences {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    margin-block: var(--space-3);
  }
  .definition-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }
  .audience {
    overflow-wrap: anywhere;
    padding: var(--space-3);
    background: var(--surface-raised);
    border-radius: var(--radius-sm);
  }
</style>
