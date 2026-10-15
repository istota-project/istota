<script lang="ts">
  import { base } from '$app/paths';
  import { page } from '$app/state';
  import {
    getProfile,
    updateProfile,
    getBriefings,
    upsertBriefing,
    deleteBriefing,
    getBriefingConfig,
    putBriefingBlock,
    getBrowsePresets,
    getFeedOptions,
    getSharedBlockOptions,
    getSharedBlocks,
    putSharedBlock,
    deleteSharedBlock,
    runSharedBlock,
    type UserBriefingRow,
    type BriefingBlock,
    type BriefingConfigResponse,
    type BrowsePreset,
    type FeedOptions,
    type SharedBlockOption,
    type SharedBlock,
  } from '$lib/api';
  import {
    Button,
    ConfirmDialog,
    EmptyState,
    IconButton,
    KebabMenu,
    Select,
    type KebabItem,
    type SelectOption,
  } from '$lib/components/ui';
  import { SettingsLayout, SettingsCard, SettingsField } from '$lib/components/settings';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';
  import GroupBriefingEditor from '$lib/components/briefings/GroupBriefingEditor.svelte';
  import BlockBuilder from '$lib/components/briefings/BlockBuilder.svelte';
  import SourceConfigFields from '$lib/components/briefings/SourceConfigFields.svelte';
  import { briefingsRefreshNonce } from '$lib/stores/briefings';
  import { getCurrentUser } from '$lib/userContext';
  import { formatDateTime } from '$lib/dateFormat';
  import { MANAGED_BADGE, isManaged } from '$lib/settings/managed';

  let groupActive = $state(false);
  let groupCopy = $state<{ briefing: UserBriefingRow; blocks: BriefingBlock[] } | null>(null);
  let creatingPersonal = $state(false);
  let loading = $state(true);
  let error = $state('');
  /* Both read the identity the root layout resolved rather than a `/me` of this
     page's own (ISSUE-355), and both decide only what is *shown*: the module
     banner below, and whether the shared-block section is drawn. Every
     authorization check behind them is server-side. */
  const identity = getCurrentUser();
  const moduleEnabled = $derived(identity.user.features.briefings);
  const isAdmin = $derived(identity.user.features.admin);

  // --- Email delivery (per-user profile preference, not per-briefing) ---
  // Lives here rather than on /settings because it only governs how a briefing
  // reaches the inbox. It writes the profile endpoint, so unlike the per-record
  // forms on this page it registers with the app bar's single Save.
  let emailHtml = $state(true);
  let emailHtmlSaved = $state(true);
  let emailHtmlSaving = $state(false);
  let emailHtmlError = $state('');
  // Set by the deployment: the profile PUT refuses an edit, so the toggle is locked.
  let emailHtmlManaged = $state(false);

  async function saveEmailHtml() {
    emailHtmlSaving = true;
    emailHtmlError = '';
    try {
      await updateProfile({ briefing_email_html: emailHtml });
      emailHtmlSaved = emailHtml;
    } catch (e) {
      emailHtmlError = (e as Error).message || 'Save failed';
    } finally {
      emailHtmlSaving = false;
    }
  }

  useSettingsSave(() =>
    moduleEnabled
      ? {
          dirty: emailHtml !== emailHtmlSaved,
          saving: emailHtmlSaving,
          save: saveEmailHtml,
        }
      : null,
  );

  // --- Schedule & delivery ---
  let briefings: UserBriefingRow[] = $state([]);
  let briefingOutputs: string[] = $state(['talk', 'email', 'ntfy', 'web']);
  let newBriefing = $state({
    name: '',
    cron: '0 7 * * *',
    title: '',
    conversation_token: '',
    output: 'talk' as string,
    enabled: true,
  });
  let briefingError = $state('');
  let briefingSaving = $state(false);
  // Non-null = editing that briefing in place. The POST is an upsert keyed on
  // name, so an edit is a save with the name held fixed.
  let briefingEditingName = $state<string | null>(null);

  const briefingOutputOptions: SelectOption[] = $derived(
    briefingOutputs.map((o) => ({ value: o, label: o })),
  );

  // --- Content blocks ---
  let config = $state<BriefingConfigResponse | null>(null);
  let presets = $state<BrowsePreset[]>([]);
  let feedOptions = $state<FeedOptions | null>(null);
  // Live pickable shared blocks (built-in + custom-published) for the Shared source.
  let sharedBlockOptions = $state<SharedBlockOption[]>([]);
  let selectedName = $state<string>('');
  let confirmDelete = $state<{ id: number; label: string } | null>(null);

  // --- Admin: shared blocks (generated once globally, read by any user) ---
  type SharedDraftSource = { kind: string; config: Record<string, unknown> };
  type SharedDraft = {
    name: string;
    cron: string;
    title: string;
    directive: string;
    render_mode: string;
    trusted: boolean;
    enabled: boolean;
    sources: SharedDraftSource[];
  };
  function emptySharedDraft(): SharedDraft {
    return {
      name: '',
      cron: '0 6 * * *',
      title: '',
      directive: '',
      render_mode: 'synthesis',
      trusted: false,
      enabled: true,
      sources: [],
    };
  }
  let sharedBlocks = $state<SharedBlock[]>([]);
  let sharedAllowedKinds = $state<string[]>(['browse', 'markets', 'email']);
  let sharedBlockTimezone = $state<string>('UTC');
  let sharedDraft = $state<SharedDraft | null>(null);
  let sharedEditingName = $state<string | null>(null); // non-null = editing existing
  let sharedError = $state('');
  let sharedSaving = $state(false);
  let sharedRunning = $state<Set<string>>(new Set());
  let confirmSharedDelete = $state<string | null>(null);

  const sharedKindOptions: SelectOption[] = $derived(
    sharedAllowedKinds.map((k) => ({ value: k, label: KIND_LABELS[k] ?? k })),
  );

  async function reloadSharedBlocks() {
    const resp = await getSharedBlocks();
    sharedBlocks = resp.shared_blocks;
    if (resp.allowed_source_kinds?.length) sharedAllowedKinds = resp.allowed_source_kinds;
    if (resp.shared_block_timezone) sharedBlockTimezone = resp.shared_block_timezone;
  }

  // Compact "last run" — the raw ISO string is wide and ugly; show a short
  // local "Mon D, HH:MM".
  const fmtLastRun = (iso: string | null) =>
    formatDateTime(iso, {
      empty: '—',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });

  function startNewShared() {
    sharedEditingName = null;
    sharedError = '';
    sharedDraft = emptySharedDraft();
  }

  function editShared(b: SharedBlock) {
    sharedEditingName = b.name;
    sharedError = '';
    sharedDraft = {
      name: b.name,
      cron: b.cron,
      title: b.title,
      directive: b.directive ?? '',
      render_mode: b.render_mode,
      trusted: b.trusted,
      enabled: b.enabled,
      sources: (b.sources ?? []).map((s) => ({
        kind: s.kind,
        config: { ...(s.config ?? {}) },
      })),
    };
  }

  // Opens the editor pre-filled with a copy under a free name rather than
  // writing immediately — the shared-block PUT is an upsert keyed on name, and
  // a definition is worth reviewing before it starts generating on a cron.
  function cloneShared(b: SharedBlock) {
    editShared(b);
    sharedEditingName = null;
    if (sharedDraft) {
      sharedDraft.name = uniqueName(
        b.name,
        sharedBlocks.map((x) => x.name),
      );
      sharedDraft.enabled = false;
    }
  }

  function sharedMenu(b: SharedBlock): KebabItem[] {
    return [
      {
        label: sharedRunning.has(b.name) ? 'Running…' : 'Run now',
        disabled: sharedRunning.has(b.name),
        onSelect: () => void doRunShared(b.name),
      },
      { label: 'Edit', onSelect: () => editShared(b) },
      { label: 'Duplicate', onSelect: () => cloneShared(b) },
      { label: 'Delete', danger: true, onSelect: () => (confirmSharedDelete = b.name) },
    ];
  }

  function cancelShared() {
    sharedDraft = null;
    sharedEditingName = null;
    sharedError = '';
  }

  function addSharedSource() {
    if (!sharedDraft) return;
    sharedDraft = {
      ...sharedDraft,
      sources: [...sharedDraft.sources, { kind: sharedAllowedKinds[0] ?? 'browse', config: {} }],
    };
  }

  function removeSharedSource(idx: number) {
    if (!sharedDraft) return;
    sharedDraft = {
      ...sharedDraft,
      sources: sharedDraft.sources.filter((_, i) => i !== idx),
    };
  }

  function setSharedSourceKind(idx: number, kind: string) {
    if (!sharedDraft) return;
    // A kind change invalidates the prior kind's config shape — reset it.
    sharedDraft.sources[idx] = { kind, config: {} };
  }

  function patchSharedSourceConfig(idx: number, patch: Record<string, unknown>) {
    if (!sharedDraft) return;
    const next = { ...sharedDraft.sources[idx].config, ...patch };
    for (const [k, v] of Object.entries(patch)) {
      if (v === undefined || v === null) delete next[k];
    }
    sharedDraft.sources[idx] = { ...sharedDraft.sources[idx], config: next };
  }

  async function saveShared() {
    if (!sharedDraft) return;
    sharedError = '';
    const name = sharedDraft.name.trim();
    if (!name) {
      sharedError = 'Name is required.';
      return;
    }
    const sources = sharedDraft.sources.map((s) => ({ kind: s.kind, config: s.config }));
    sharedSaving = true;
    try {
      await putSharedBlock({
        name,
        cron: sharedDraft.cron.trim(),
        title: sharedDraft.title.trim(),
        directive: sharedDraft.directive.trim() || null,
        render_mode: sharedDraft.render_mode,
        trusted: sharedDraft.trusted,
        enabled: sharedDraft.enabled,
        sources,
      });
      await reloadSharedBlocks();
      sharedDraft = null;
      sharedEditingName = null;
    } catch (e) {
      sharedError = (e as Error).message || 'Save failed';
    } finally {
      sharedSaving = false;
    }
  }

  // The daemon generates the block on its next tick (ISSUE-700). A gather
  // can take a minute and the model call has a three-minute ceiling, so the
  // wait is generous, and running out of it is reported as not yet answered.
  const SHARED_RUN_POLL_MS = 1500;
  const SHARED_RUN_WAIT_MS = 300_000;
  const SHARED_RUN_OUTCOMES: Record<string, string> = {
    kept_prior: 'Nothing was generated; the previous content was kept.',
    brain_unavailable: 'The primary model is unavailable; the previous content was kept.',
    failed: 'Run failed; see the daemon log.',
    not_found: 'The block was deleted before it ran.',
  };

  async function doRunShared(name: string) {
    sharedRunning = new Set(sharedRunning).add(name);
    try {
      const asked = await runSharedBlock(name);
      const until = Date.now() + SHARED_RUN_WAIT_MS;
      while (Date.now() < until) {
        await new Promise((resolve) => setTimeout(resolve, SHARED_RUN_POLL_MS));
        await reloadSharedBlocks();
        const block = sharedBlocks.find((b) => b.name === name);
        if (!block) {
          sharedError = `${name}: ${SHARED_RUN_OUTCOMES.not_found}`;
          return;
        }
        const answer = block.status.run_answered;
        if (answer?.id === asked.id) {
          const message = SHARED_RUN_OUTCOMES[answer.outcome];
          if (message) sharedError = `${name}: ${message}`;
          return;
        }
      }
      sharedError =
        `${name}: the daemon has not answered yet. It may still be generating; ` +
        'if this persists, check that the daemon is running.';
    } catch (e) {
      sharedError = (e as Error).message || 'Run failed';
    } finally {
      const next = new Set(sharedRunning);
      next.delete(name);
      sharedRunning = next;
    }
  }

  async function doDeleteShared(name: string) {
    try {
      await deleteSharedBlock(name);
      await reloadSharedBlocks();
    } catch (e) {
      sharedError = (e as Error).message || 'Delete failed';
    } finally {
      confirmSharedDelete = null;
    }
  }

  const KIND_LABELS: Record<string, string> = {
    rss: 'RSS feed',
    email: 'Newsletters',
    browse: 'Web page',
    markets: 'Markets',
    calendar: 'Calendar',
    todos: 'Todos',
    reminders: 'Reminders',
    notes: 'Notes',
    shared_block: 'Shared block',
  };
  const RENDER_OPTIONS: SelectOption[] = [
    { value: 'synthesis', label: 'Synthesis (summarize)' },
    { value: 'structured', label: 'Structured (verbatim)' },
  ];

  const sourceKinds = $derived(config?.source_kinds ?? Object.keys(KIND_LABELS));

  const allNames = $derived.by(() => {
    if (!config) return [] as string[];
    const set = new Set<string>();
    briefings.forEach((briefing) => set.add(briefing.name));
    config.schedule_names.forEach((n) => set.add(n));
    config.briefings.forEach((b) => set.add(b.name));
    return [...set];
  });

  const selectedSchedule = $derived(briefings.find((briefing) => briefing.name === selectedName));

  const currentBlocks = $derived.by(() => {
    if (!config) return [] as BriefingBlock[];
    return config.briefings.find((b) => b.name === selectedName)?.blocks ?? [];
  });

  const rssOptions: SelectOption[] = $derived([
    ...(feedOptions?.categories ?? []).map((c) => ({
      value: `category:${c.value}`,
      label: `Category: ${c.label}`,
    })),
    ...(feedOptions?.subscriptions ?? []).map((s) => ({
      value: `subscription:${s.value}`,
      label: `Feed: ${s.label}`,
    })),
  ]);
  const browseOptions: SelectOption[] = $derived([
    { value: '__custom__', label: 'Custom URL…' },
    ...presets.map((p) => ({ value: `preset:${p.key}`, label: p.name })),
  ]);
  const sharedBlockSelectOptions: SelectOption[] = $derived(
    sharedBlockOptions.map((o) => ({
      value: o.name,
      label: o.source === 'custom' ? `${o.name} (custom)` : o.name,
    })),
  );

  async function reloadSchedule() {
    const resp = await getBriefings();
    briefings = resp.briefings;
    briefingOutputs = resp.outputs?.length ? resp.outputs : ['talk', 'email', 'ntfy', 'web'];
  }

  async function reloadContent() {
    config = await getBriefingConfig();
    if (!selectedName && allNames.length) {
      const requested = page.url.searchParams.get('name');
      selectedName = requested && allNames.includes(requested) ? requested : allNames[0];
    }
  }

  /* Driven by the gate rather than by the mount, because the gate is reactive
     now. A page that mounts under a cached identity whose `features` differ
     from the live one — offline, then the connection returns — would otherwise
     have the banner replaced by a fully drawn configuration UI with none of its
     data ever fetched, `loading` already false and no reload affordance to
     recover with. The flags are `$derived`, so this is where the load has to
     hang; guarded so the ordinary case still loads exactly once. */
  let loadStarted = false;
  $effect(() => {
    if (!moduleEnabled || loadStarted) return;
    loadStarted = true;
    void load();
  });

  /* Its own hook for the same reason, and separate because the two gates move
     independently: a record can gain `admin` without gaining `briefings`. */
  let sharedBlocksStarted = false;
  $effect(() => {
    if (!isAdmin || sharedBlocksStarted) return;
    sharedBlocksStarted = true;
    reloadSharedBlocks().catch(() => (sharedBlocks = []));
  });

  /* The module being off is a settled answer, not a pending one: without this
     the disabled banner would sit under `SettingsLayout`'s loading state for
     the life of the page, since nothing else clears it on that path. */
  $effect(() => {
    if (!moduleEnabled) loading = false;
  });

  async function load() {
    try {
      await Promise.all([
        reloadSchedule(),
        (async () => {
          [presets, feedOptions] = await Promise.all([
            getBrowsePresets().then((r) => r.presets),
            getFeedOptions(),
          ]);
          await reloadContent();
        })(),
        getSharedBlockOptions()
          .then((r) => (sharedBlockOptions = r.options))
          .catch(() => (sharedBlockOptions = [])),
        getProfile()
          .then((r) => {
            emailHtml = r.profile?.briefing_email_html !== false;
            emailHtmlSaved = emailHtml;
            emailHtmlManaged = isManaged(r.profile, 'briefing_email_html');
          })
          .catch(() => {}),
      ]);
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to load settings';
    } finally {
      loading = false;
    }
  }

  /** Preview of the title a blank `title` field will produce.
   *
   * Display hint only — the real value comes from
   * `briefings.generate.default_briefing_title`, which is authoritative. Keep
   * the two in step. The run date the renderer appends is not shown here.
   */
  function derivedTitle(name: string): string {
    const words = name
      .trim()
      .split(/[-_\s]+/)
      .filter(Boolean);
    if (!words.length) return 'Briefing';
    const label = words.map((w) => w[0].toUpperCase() + w.slice(1)).join(' ');
    return label.toLowerCase().includes('brief') ? label : `${label} Briefing`;
  }

  // ---- Schedule handlers ----
  function resetBriefingForm() {
    newBriefing = {
      name: '',
      cron: '0 7 * * *',
      title: '',
      conversation_token: '',
      output: 'talk',
      enabled: true,
    };
    briefingEditingName = null;
    briefingError = '';
  }

  function editBriefing(b: UserBriefingRow) {
    briefingEditingName = b.name;
    briefingError = '';
    newBriefing = {
      name: b.name,
      cron: b.cron,
      title: b.title ?? '',
      conversation_token: b.conversation_token ?? '',
      output: b.output,
      enabled: b.enabled,
    };
  }

  // A clone must not collide with an existing name: the briefing/shared-block
  // endpoints are upserts keyed on name, so a colliding "copy" would silently
  // overwrite the original rather than fail.
  function uniqueName(base: string, taken: string[]): string {
    const used = new Set(taken);
    if (!used.has(`${base}-copy`)) return `${base}-copy`;
    for (let n = 2; ; n += 1) {
      const candidate = `${base}-copy-${n}`;
      if (!used.has(candidate)) return candidate;
    }
  }

  // Deep clone: the schedule row plus every block and each block's sources.
  // Cloning the schedule alone would look successful while producing an empty
  // briefing, since blocks are keyed by briefing_name.
  async function cloneBriefing(b: UserBriefingRow) {
    const name = uniqueName(
      b.name,
      briefings.map((x) => x.name),
    );
    briefingError = '';
    briefingSaving = true;
    try {
      await upsertBriefing({
        name,
        cron: b.cron,
        title: b.title ?? '',
        conversation_token: b.conversation_token ?? undefined,
        output: b.output,
        // A clone starts muted so a duplicated schedule can't fire before it
        // has been reviewed and renamed.
        enabled: false,
      });
      const blocks = config?.briefings.find((x) => x.name === b.name)?.blocks ?? [];
      for (const block of blocks) {
        await putBriefingBlock({
          briefing_name: name,
          title: block.title,
          directive: block.directive ?? '',
          render_mode: block.render_mode,
          options: block.options ?? {},
          // Legacy empty blocks remain copyable through the block-only API.
          ...(block.sources.length
            ? {
                sources: block.sources.map((source) => ({
                  kind: source.kind,
                  config: source.config ?? {},
                  enabled: source.enabled,
                })),
              }
            : {}),
        });
      }
      await Promise.all([reloadSchedule(), reloadContent()]);
      briefingsRefreshNonce.update((n) => n + 1);
    } catch (e) {
      briefingError = (e as Error).message || 'Clone failed';
    } finally {
      briefingSaving = false;
    }
  }

  function briefingMenu(b: UserBriefingRow): KebabItem[] {
    if (b.managed !== 'db' || b.id === undefined) return [];
    return [
      { label: 'Edit', onSelect: () => editBriefing(b) },
      { label: 'Duplicate', onSelect: () => void cloneBriefing(b) },
      {
        label: 'Duplicate for group',
        onSelect: () => {
          groupCopy = {
            briefing: b,
            blocks: config?.briefings.find((item) => item.name === b.name)?.blocks ?? [],
          };
        },
      },
      {
        label: 'Delete',
        danger: true,
        onSelect: () => (confirmDelete = { id: b.id!, label: b.name }),
      },
    ];
  }

  async function submitBriefing(e: SubmitEvent) {
    e.preventDefault();
    if (briefingSaving) return;
    briefingError = '';
    const name = newBriefing.name.trim();
    const cron = newBriefing.cron.trim();
    if (!name || !cron) {
      briefingError = 'Name and cron are required.';
      return;
    }
    if (newBriefing.output === 'talk' && !newBriefing.conversation_token.trim()) {
      briefingError = `Conversation token is required when output is "${newBriefing.output}".`;
      return;
    }
    briefingSaving = true;
    try {
      await upsertBriefing({
        name,
        cron,
        title: newBriefing.title.trim(),
        conversation_token: newBriefing.conversation_token.trim() || undefined,
        output: newBriefing.output,
        enabled: newBriefing.enabled,
      });
      resetBriefingForm();
      await Promise.all([reloadSchedule(), reloadContent()]);
      briefingsRefreshNonce.update((n) => n + 1);
    } catch (e) {
      briefingError = (e as Error).message || 'Save failed';
    } finally {
      briefingSaving = false;
    }
  }

  function updateBlocks(name: string, blocks: BriefingBlock[]) {
    if (!config) return;
    const others = config.briefings.filter((briefing) => briefing.name !== name);
    config = { ...config, briefings: [...others, { name, blocks }] };
  }

  async function performDelete() {
    if (!confirmDelete || briefingSaving) return;
    const target = confirmDelete;
    briefingSaving = true;
    try {
      await deleteBriefing(target.id);
      await Promise.all([reloadSchedule(), reloadContent()]);
      if (!allNames.includes(selectedName)) selectedName = allNames[0] ?? '';
      confirmDelete = null;
      briefingsRefreshNonce.update((n) => n + 1);
    } catch (e) {
      briefingError = (e as Error).message || 'Delete failed';
    } finally {
      briefingSaving = false;
    }
  }
</script>

<svelte:head>
  <title>Briefings settings</title>
</svelte:head>

<SettingsLayout
  title="Briefings settings"
  description="Schedule and deliver briefings, and shape their content blocks. A briefing runs on its cron in your timezone and is synthesized from the blocks below."
  {loading}
  {error}
>
  <GroupBriefingEditor
    templates={config?.block_templates ?? []}
    {browseOptions}
    sharedBlockOptions={sharedBlockSelectOptions}
    copy={groupCopy}
    onGroupActive={(active) => {
      groupActive = active;
    }}
    onPersonalNew={() => {
      resetBriefingForm();
      newBriefing.enabled = false;
      creatingPersonal = true;
    }}
  />
  {#if !groupActive}
    {#if !moduleEnabled}
      <div class="banner info">
        Briefings module is disabled. Enable it in
        <a href="{base}/settings">Settings → Preferences</a> to manage schedules and content.
      </div>
    {:else}
      <SettingsCard
        title="Briefing"
        description="Choose a briefing to edit its schedule and ordered content blocks."
      >
        {#if config}
          <SettingsField label="Briefing" labelled={false}>
            <Select
              value={selectedName}
              options={allNames.map((name) => ({
                value: name,
                label: `${name}${briefings.find((briefing) => briefing.name === name)?.enabled === false ? ' (disabled)' : ''}`,
              }))}
              onValueChange={(value) => {
                selectedName = value;
              }}
              ariaLabel="Briefing"
              fullWidth
            />
          </SettingsField>
          {#if selectedName}
            <div class="schedule-summary" aria-label="Selected briefing schedule">
              {#if selectedSchedule}
                <div class="schedule-text">
                  <strong>{selectedSchedule.title || derivedTitle(selectedName)}</strong>
                  <p class="caption">
                    <code>{selectedSchedule.cron}</code> · {selectedSchedule.output}{selectedSchedule.enabled
                      ? ''
                      : ' · Disabled'}
                  </p>
                  {#if selectedSchedule.conversation_token}<p class="caption">
                      Talk room: {selectedSchedule.conversation_token}
                    </p>{/if}
                  {#if selectedSchedule.managed === 'config'}<p class="caption">
                      Schedule managed by config.toml.
                    </p>{/if}
                </div>
                {#if selectedSchedule.managed === 'db' && selectedSchedule.id !== undefined}
                  <Button
                    size="sm"
                    variant="secondary"
                    onclick={() => selectedSchedule && editBriefing(selectedSchedule)}
                    >Edit schedule</Button
                  >
                  <KebabMenu items={briefingMenu(selectedSchedule)} ariaLabel="Briefing actions" />
                {/if}
              {:else}<p class="caption">
                  No schedule. Add one below using this briefing's name.
                </p>{/if}
            </div>
            {#key selectedName}
              <BlockBuilder
                briefingName={selectedName}
                editBlockId={page.url.searchParams.get('name') === selectedName
                  ? Number(page.url.searchParams.get('block')) || null
                  : null}
                blocks={currentBlocks}
                templates={config.block_templates ?? []}
                {sourceKinds}
                {browseOptions}
                {rssOptions}
                sharedBlockOptions={sharedBlockSelectOptions}
                feedsAvailable={!!feedOptions?.available && rssOptions.length > 0}
                onChange={updateBlocks}
              />
            {/key}
          {:else}<EmptyState>No briefings scheduled yet. Add one below.</EmptyState>{/if}
        {/if}
        <details
          class="schedule-form"
          open={creatingPersonal || !!briefingEditingName || allNames.length === 0}
        >
          <summary
            >{briefingEditingName
              ? `Edit ${briefingEditingName} schedule`
              : 'Add briefing'}</summary
          >
          <form class="add-form" onsubmit={submitBriefing}>
            <h3>{briefingEditingName ? `Edit ${briefingEditingName}` : 'Add briefing'}</h3>
            <div class="add-grid">
              <SettingsField label="Name">
                <input
                  type="text"
                  placeholder="morning"
                  bind:value={newBriefing.name}
                  disabled={!!briefingEditingName}
                />
              </SettingsField>
              <SettingsField
                label="Title"
                hint="Shown as the email subject and the archive entry. Leave it blank to derive one from the name. The run date is appended automatically."
              >
                <input
                  type="text"
                  placeholder={derivedTitle(newBriefing.name || 'morning')}
                  bind:value={newBriefing.title}
                />
              </SettingsField>
              <SettingsField label="Cron (user TZ)">
                <input type="text" placeholder="0 7 * * 1-5" bind:value={newBriefing.cron} />
              </SettingsField>
              <SettingsField label="Output">
                <Select
                  value={newBriefing.output}
                  options={briefingOutputOptions}
                  onValueChange={(v) => (newBriefing.output = v)}
                  ariaLabel="Output"
                  fullWidth
                />
              </SettingsField>
              <SettingsField label="Conversation token">
                <input
                  type="text"
                  placeholder="Talk room token"
                  bind:value={newBriefing.conversation_token}
                />
              </SettingsField>
              <SettingsField label="Enabled" checkbox>
                <input type="checkbox" bind:checked={newBriefing.enabled} />
              </SettingsField>
            </div>
            <div class="add-actions">
              <Button variant="secondary" size="sm" type="submit" disabled={briefingSaving}>
                {briefingSaving
                  ? 'Saving…'
                  : briefingEditingName
                    ? 'Save changes'
                    : '+ Add briefing'}
              </Button>
              {#if briefingEditingName}
                <Button variant="secondary" size="sm" onclick={resetBriefingForm}>Cancel</Button>
              {/if}
            </div>
            {#if briefingError}
              <div class="banner error">{briefingError}</div>
            {/if}
          </form>
        </details>
      </SettingsCard>

      <SettingsCard
        title="Email delivery"
        description="Applies to every briefing you have delivered by email."
      >
        <SettingsField
          label="Send as HTML"
          checkbox
          badge={emailHtmlManaged ? MANAGED_BADGE : undefined}
          hint="Renders the briefing as formatted mail, so a news source that supplied an article URL becomes a clickable link. A plain-text copy is always sent alongside it for clients that prefer one. Untick for plain text only."
          error={emailHtmlError}
        >
          <input type="checkbox" bind:checked={emailHtml} disabled={emailHtmlManaged} />
        </SettingsField>
      </SettingsCard>

      {#if isAdmin}
        <SettingsCard
          title="Shared blocks ({sharedBlocks.length})"
          description="Admin only. Content generated once globally and read by any user via a Shared source. Structured blocks are stored verbatim (no LLM). Only user-agnostic sources are allowed."
        >
          {#if sharedError}
            <div class="banner error">{sharedError}</div>
          {/if}

          {#if sharedBlocks.length === 0}
            <EmptyState>No shared blocks yet.</EmptyState>
          {:else}
            <div class="table-scroll">
              <table class="grid sb-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Cron ({sharedBlockTimezone})</th>
                    <th class="col-render">Render</th>
                    <th>Trust</th>
                    <th>Last run</th>
                    <th class="actions"></th>
                  </tr>
                </thead>
                <tbody>
                  {#each sharedBlocks as b (b.name)}
                    <tr>
                      <td class="sb-name">
                        <strong>{b.name}</strong>
                        {#if !b.enabled}<span class="muted small"> (disabled)</span>{/if}
                        {#if b.status.has_content}
                          <div class="muted small sb-preview" title={b.status.value_preview ?? ''}>
                            {(b.status.value_preview ?? '').slice(0, 60)}
                          </div>
                        {/if}
                      </td>
                      <td class="nowrap"><code>{b.cron}</code></td>
                      <td class="col-render"><span class="render-pill">{b.render_mode}</span></td>
                      <td class="small">{b.trusted ? 'trusted' : 'untrusted'}</td>
                      <td class="small nowrap">{fmtLastRun(b.status.last_run_at)}</td>
                      <td class="actions">
                        <KebabMenu items={sharedMenu(b)} ariaLabel="Shared block actions" />
                      </td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          {/if}

          {#if !sharedDraft}
            <div style="margin-top: 0.75rem;">
              <Button variant="secondary" size="sm" onclick={startNewShared}
                >+ New shared block</Button
              >
            </div>
          {:else}
            <div class="shared-editor">
              <h4>{sharedEditingName ? `Edit ${sharedEditingName}` : 'New shared block'}</h4>
              <div class="sb-grid">
                <SettingsField label="Name" hint="Slug: lowercase, digits, - and _">
                  <input
                    type="text"
                    placeholder="world-headlines"
                    bind:value={sharedDraft.name}
                    disabled={!!sharedEditingName}
                  />
                </SettingsField>
                <SettingsField label={`Cron (${sharedBlockTimezone})`}>
                  <input type="text" placeholder="0 6 * * *" bind:value={sharedDraft.cron} />
                </SettingsField>
              </div>
              <SettingsField label="Title">
                <input
                  type="text"
                  placeholder="🌍 World headlines"
                  bind:value={sharedDraft.title}
                />
              </SettingsField>
              <SettingsField label="Render mode">
                <Select
                  value={sharedDraft.render_mode}
                  options={RENDER_OPTIONS}
                  onValueChange={(v) => sharedDraft && (sharedDraft.render_mode = v)}
                  ariaLabel="Render mode"
                  fullWidth
                />
              </SettingsField>
              {#if sharedDraft.render_mode === 'synthesis'}
                <SettingsField label="Directive" hint="How to synthesize the sources.">
                  <textarea rows="2" bind:value={sharedDraft.directive}></textarea>
                </SettingsField>
              {/if}
              <div class="sb-grid">
                <SettingsField label="Trusted" checkbox hint="Only for injection-safe content.">
                  <input type="checkbox" bind:checked={sharedDraft.trusted} />
                </SettingsField>
                <SettingsField label="Enabled" checkbox>
                  <input type="checkbox" bind:checked={sharedDraft.enabled} />
                </SettingsField>
              </div>

              <div class="shared-sources">
                <div class="shared-sources-head">
                  <span class="field-label">Sources</span>
                  <Button variant="secondary" size="sm" onclick={addSharedSource}
                    >+ Add source</Button
                  >
                </div>
                {#if sharedDraft.sources.length === 0}
                  <p class="muted small">
                    No sources — add at least one browse/markets/email source.
                  </p>
                {/if}
                {#each sharedDraft.sources as src, i (i)}
                  <div class="sb-source">
                    <span class="sb-source-remove">
                      <IconButton
                        label="Remove source {i + 1}"
                        title="Remove source"
                        size="sm"
                        danger
                        onclick={() => removeSharedSource(i)}>×</IconButton
                      >
                    </span>
                    <SettingsField label="Type">
                      <Select
                        value={src.kind}
                        options={sharedKindOptions}
                        onValueChange={(v) => setSharedSourceKind(i, v)}
                        ariaLabel="Source kind"
                        fullWidth
                      />
                    </SettingsField>
                    <SourceConfigFields
                      kind={src.kind}
                      config={src.config}
                      onChange={(patch) => patchSharedSourceConfig(i, patch)}
                      {browseOptions}
                    />
                  </div>
                {/each}
              </div>

              <div class="source-form-actions">
                <Button variant="secondary" size="sm" onclick={cancelShared}>Cancel</Button>
                <Button variant="primary" size="sm" onclick={saveShared} disabled={sharedSaving}
                  >{sharedSaving ? 'Saving…' : 'Save shared block'}</Button
                >
              </div>
            </div>
          {/if}
        </SettingsCard>
      {/if}
    {/if}
  {/if}
</SettingsLayout>

{#if confirmSharedDelete}
  <ConfirmDialog
    open={true}
    title="Delete shared block"
    confirmLabel="Delete"
    onConfirm={() => confirmSharedDelete && doDeleteShared(confirmSharedDelete)}
    onCancel={() => (confirmSharedDelete = null)}
  >
    {#snippet body()}
      <p>
        Are you sure you want to delete the shared block <strong>{confirmSharedDelete}</strong>? The
        last generated value stays until it goes stale. Users referencing it lose the section once
        it expires.
      </p>
    {/snippet}
  </ConfirmDialog>
{/if}

{#if confirmDelete}
  <ConfirmDialog
    open={true}
    title="Remove briefing"
    message={`Are you sure you want to remove the briefing "${confirmDelete.label}"?`}
    confirmLabel="Remove"
    confirmDisabled={briefingSaving}
    onConfirm={performDelete}
    onCancel={() => (confirmDelete = null)}
  />
{/if}

<style>
  /* Shared .settings/.card/.field/.grid/.banner/.icon-btn/.muted
	   primitives live in web/src/lib/styles/settings.css. Only page-specific
	   layout stays here. */

  .add-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    padding-top: var(--space-2);
    margin-top: var(--space-2);
    border-top: 1px solid var(--border-subtle);
  }

  .add-form h3 {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-muted);
  }

  .add-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(160px, 100%), 1fr));
    gap: var(--space-2);
    /* Bottom-align so the Enabled checkbox row lines up with the other
		   fields' inputs (which sit below their labels), not their labels. */
    align-items: end;
  }

  /* Nudge the checkbox down so it visually rests on the inputs' baseline
	   row rather than floating a touch high in its bottom-aligned cell. */
  .add-grid :global(.field.checkbox) {
    padding-bottom: var(--space-2);
  }

  .add-actions {
    display: flex;
    justify-content: flex-start;
  }

  .schedule-summary {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-3);
    margin: var(--space-3) 0;
  }
  .schedule-text {
    flex: 1;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .schedule-text p {
    margin: var(--space-1) 0;
  }
  .schedule-form {
    margin-top: var(--space-4);
    border-top: 1px solid var(--border-subtle);
    padding-top: var(--space-3);
  }
  .schedule-form summary {
    cursor: pointer;
    font-size: var(--text-sm);
  }
  .col-render {
    width: 7rem;
  }
  .render-pill {
    display: inline-block;
    font-size: var(--text-xs);
    padding: 0.05rem var(--space-2);
    border-radius: var(--radius-pill);
    background: var(--surface-raised);
    color: var(--text-muted);
  }
  .muted.small {
    font-size: var(--text-xs);
  }
  .source-form-actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-2);
    margin-top: var(--space-1);
  }

  /* Admin shared-blocks editor */
  .nowrap {
    white-space: nowrap;
  }
  /* The global .grid is table-layout:fixed/width:100%, which squishes this
	   table's six content columns on mobile. Keep a natural min-width so
	   .table-scroll scrolls horizontally instead of overlapping columns. The
	   floor used to be 44rem to fit three text action buttons; the actions are a
	   kebab now, so only the content columns set it. */
  .sb-table {
    table-layout: auto;
    min-width: 34rem;
  }
  .sb-table td.actions,
  .sb-table th.actions {
    width: 1%;
    white-space: nowrap;
  }
  .sb-name {
    min-width: 9rem;
  }
  .sb-preview {
    max-width: 16rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .shared-editor {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    margin-top: var(--space-3);
    padding: var(--space-4);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    background: var(--surface-base);
  }
  .shared-editor h4 {
    margin: 0;
    font-size: var(--text-sm);
    font-weight: 600;
    color: var(--text-primary);
  }
  /* Two-column field row that top-aligns, so a field with a hint doesn't push
	   its sibling's input out of line (unlike .inline-fields' flex-end). */
  .sb-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--space-2) 1.25rem;
    align-items: start;
  }
  .shared-sources {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }
  .shared-sources-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .field-label {
    font-size: var(--text-sm);
    color: var(--text-muted);
  }
  /* Type + the source's config fields flow inline by default, wrapping on
	   narrow widths. The remove button pins to the top-right corner. */
  .sb-source {
    position: relative;
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: var(--space-2) var(--space-4);
    padding: var(--space-3) 2.25rem var(--space-3) var(--space-3);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    background: var(--surface-base);
  }
  .sb-source :global(.field) {
    flex: 1 1 12rem;
    min-width: 10rem;
  }
  /* Full-width notes / helper text (e.g. the markets "leave blank" hint) break
	   the inline row rather than squeezing between the selects. */
  .sb-source :global(p) {
    flex-basis: 100%;
    margin: 0;
  }
  /* A positioning wrapper, so the IconButton inside keeps its own padding and
     hit area rather than having them overwritten to place it. */
  .sb-source-remove {
    position: absolute;
    top: 0.35rem;
    right: 0.4rem;
    font-size: 1.25rem;
    line-height: 1;
  }
  @container settings (max-width: 560px) {
    .sb-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
