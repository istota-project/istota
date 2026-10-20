<script lang="ts">
  import { createConfirm } from '$lib/ui/confirm.svelte';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import {
    getProfile,
    updateProfile,
    getBriefings,
    upsertBriefing,
    deleteBriefing,
    deleteGroupBriefing,
    getBriefingConfig,
    getBriefingTargets,
    putBriefingBlock,
    getBrowsePresets,
    getFeedOptions,
    getSharedBlockOptions,
    getEmailAccounts,
    type BriefingBlock,
    type BriefingConfigResponse,
    type BriefingRoomOption,
    type BriefingTarget,
    type BrowsePreset,
    type FeedOptions,
    type SharedBlockOption,
    type EmailAccount,
    type UserBriefingRow,
  } from '$lib/api';
  import {
    Alert,
    ConfirmDialog,
    EmptyState,
    KebabMenu,
    type KebabItem,
    Card,
    Field,
    Checkbox,
  } from '$lib/components/ui';
  import { SettingsLayout } from '$lib/components/settings';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';
  import { briefingsRefreshNonce } from '$lib/stores/briefings';
  import { notifyError } from '$lib/stores/notices';
  import { getCurrentUser } from '$lib/userContext';
  import { MANAGED_BADGE, isManaged } from '$lib/settings/managed';
  import GroupBriefingEditor from '$lib/components/briefings/GroupBriefingEditor.svelte';
  import GroupDeliveryPreferences from '$lib/components/briefings/GroupDeliveryPreferences.svelte';
  import NewBriefing from '$lib/components/briefings/settings/NewBriefing.svelte';
  import PersonalBriefingEditor from '$lib/components/briefings/settings/PersonalBriefingEditor.svelte';
  import SharedBlocksSettings from '$lib/components/briefings/settings/SharedBlocksSettings.svelte';
  import DuplicateToGroupDialog, {
    type DuplicateSource,
  } from '$lib/components/briefings/settings/DuplicateToGroupDialog.svelte';
  import {
    briefingSettingsUrl,
    channelLabel,
    derivedTitle,
    uniqueName,
  } from '$lib/components/briefings/settings/briefingSettings';

  /* Both read the identity the root layout resolved rather than a `/me` of this
     page's own (ISSUE-355), and both decide only what is *shown*. Every
     authorization check behind them is server-side. */
  const identity = getCurrentUser();
  const moduleEnabled = $derived(identity.user.features.briefings);
  const isAdmin = $derived(identity.user.features.admin);

  // Everything is addressed by URL, so the sidebar, links from the reader and
  // Groups, and Back all agree: `view` picks a section, `name` a personal
  // briefing, `group` + `briefing` a group one, and `new` the create flow.
  const params = $derived(page.url.searchParams);
  const view = $derived(params.get('view') ?? '');
  const creating = $derived(params.has('new'));
  const groupId = $derived(creating ? '' : (params.get('group') ?? ''));
  const groupName = $derived(params.get('briefing') ?? '');

  let loading = $state(true);
  let error = $state('');
  let briefings = $state<UserBriefingRow[]>([]);
  let outputs = $state<string[]>(['talk', 'email', 'ntfy', 'web']);
  let rooms = $state<BriefingRoomOption[]>([]);
  let config = $state<BriefingConfigResponse | null>(null);
  let presets = $state<BrowsePreset[]>([]);
  let feedOptions = $state<FeedOptions | null>(null);
  let sharedBlockOptions = $state<SharedBlockOption[]>([]);
  let emailAccounts = $state<EmailAccount[]>([]);
  let targets = $state<BriefingTarget[]>([]);
  let channels = $state<string[]>(['in_app']);
  let deploymentTimezone = $state('UTC');

  // --- Email format (a profile preference, not per briefing) ---
  let emailHtml = $state(true);
  let emailHtmlSaved = $state(true);
  let emailHtmlSaving = $state(false);
  let emailHtmlManaged = $state(false);

  async function saveEmailHtml() {
    emailHtmlSaving = true;
    try {
      await updateProfile({ briefing_email_html: emailHtml });
      emailHtmlSaved = emailHtml;
    } catch (e) {
      notifyError((e as Error).message || 'Could not save the email format.');
    } finally {
      emailHtmlSaving = false;
    }
  }

  useSettingsSave(() =>
    moduleEnabled && view === 'delivery'
      ? { dirty: emailHtml !== emailHtmlSaved, saving: emailHtmlSaving, save: saveEmailHtml }
      : null,
  );

  const allNames = $derived.by(() => {
    if (!config) return [] as string[];
    const set = new Set<string>();
    briefings.forEach((briefing) => set.add(briefing.name));
    config.schedule_names.forEach((n) => set.add(n));
    config.briefings.forEach((b) => set.add(b.name));
    return [...set];
  });
  const personalName = $derived.by(() => {
    if (view || creating || groupId) return '';
    const requested = params.get('name');
    if (requested && allNames.includes(requested)) return requested;
    return allNames[0] ?? '';
  });
  const schedule = $derived(briefings.find((b) => b.name === personalName));
  const personalBlocks = $derived(
    config?.briefings.find((b) => b.name === personalName)?.blocks ?? [],
  );
  const groupTarget = $derived(targets.find((t) => t.group_id === groupId));

  const sourceKinds = $derived(config?.source_kinds ?? []);
  const rssOptions = $derived([
    ...(feedOptions?.categories ?? []).map((c) => ({
      value: `category:${c.value}`,
      label: `Category: ${c.label}`,
    })),
    ...(feedOptions?.subscriptions ?? []).map((s) => ({
      value: `subscription:${s.value}`,
      label: `Feed: ${s.label}`,
    })),
  ]);
  const browseOptions = $derived([
    { value: '__custom__', label: 'Custom URL…' },
    ...presets.map((p) => ({ value: `preset:${p.key}`, label: p.name })),
  ]);
  const sharedBlockSelectOptions = $derived(
    sharedBlockOptions.map((o) => ({
      value: o.name,
      label: o.source === 'custom' ? `${o.name} (custom)` : o.name,
    })),
  );

  async function reloadSchedule() {
    const resp = await getBriefings();
    briefings = resp.briefings;
    if (resp.outputs?.length) outputs = resp.outputs;
    rooms = resp.rooms ?? [];
  }
  async function reloadContent() {
    config = await getBriefingConfig();
  }

  /* Driven by the gate rather than by the mount: a page that mounts under a
     cached identity whose `features` differ from the live one must still load
     once the gate opens. Guarded so the ordinary case loads exactly once. */
  let loadStarted = false;
  $effect(() => {
    if (!moduleEnabled || loadStarted) return;
    loadStarted = true;
    void load();
  });
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
        getBriefingTargets()
          .then((r) => {
            targets = r.targets;
            channels = r.channels;
            deploymentTimezone = r.timezone;
          })
          .catch(() => {}),
        getSharedBlockOptions()
          .then((r) => (sharedBlockOptions = r.options))
          .catch(() => (sharedBlockOptions = [])),
        getEmailAccounts()
          .then((r) => (emailAccounts = r.accounts))
          .catch(() => (emailAccounts = [])),
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

  function updateBlocks(name: string, blocks: BriefingBlock[]) {
    if (!config) return;
    const others = config.briefings.filter((briefing) => briefing.name !== name);
    config = { ...config, briefings: [...others, { name, blocks }] };
  }

  // --- Header actions ---
  let groupEditor = $state<GroupBriefingEditor | null>(null);
  let duplicating = $state<{ from: DuplicateSource; exclude: string } | null>(null);
  const confirmDelete = createConfirm<UserBriefingRow>();
  const confirmGroupDelete = createConfirm<{ revision: number; title: string }>();
  let busy = $state(false);

  function personalDuplicateSource(row: UserBriefingRow): DuplicateSource {
    return {
      name: row.name,
      title: row.title || derivedTitle(row.name),
      cron: row.cron,
      timezone: deploymentTimezone,
      channel: channels.includes('in_app') ? 'in_app' : (channels[0] ?? 'in_app'),
      excluded_user_ids: [],
      mode: 'blocks',
      prompt: '',
      research: { enabled: false, prompt: '' },
      containsPersonalInformation: false,
      blocks: personalBlocks.map((b) => ({
        title: b.title,
        directive: b.directive || null,
        render_mode: b.render_mode,
        options: b.options ?? {},
        sources: b.sources.map((s) => ({
          kind: s.kind,
          config: s.config ?? {},
          enabled: s.enabled,
        })),
      })),
    };
  }

  // A deep copy: the schedule row plus every block and its sources, because
  // blocks are keyed by briefing name and a schedule alone would be empty.
  async function clonePersonal(row: UserBriefingRow) {
    const name = uniqueName(row.name, allNames);
    busy = true;
    try {
      await upsertBriefing({
        name,
        cron: row.cron,
        title: row.title ? `${row.title} (copy)` : '',
        conversation_token: row.conversation_token || undefined,
        output: row.output,
        enabled: false,
      });
      for (const block of personalBlocks)
        await putBriefingBlock({
          briefing_name: name,
          title: block.title,
          directive: block.directive ?? '',
          render_mode: block.render_mode,
          options: block.options ?? {},
          ...(block.sources.length
            ? {
                sources: block.sources.map((s) => ({
                  kind: s.kind,
                  config: s.config ?? {},
                  enabled: s.enabled,
                })),
              }
            : {}),
        });
      await Promise.all([reloadSchedule(), reloadContent()]);
      briefingsRefreshNonce.update((n) => n + 1);
      await goto(briefingSettingsUrl({ kind: 'personal', name }));
    } catch (e) {
      notifyError((e as Error).message || 'Could not duplicate the briefing.');
    } finally {
      busy = false;
    }
  }

  async function performDelete() {
    const row = confirmDelete.target;
    confirmDelete.cancel();
    if (!row?.id || busy) return;
    busy = true;
    try {
      await deleteBriefing(row.id);
      await Promise.all([reloadSchedule(), reloadContent()]);
      briefingsRefreshNonce.update((n) => n + 1);
      await goto(`${base}/briefings/settings/`, { replaceState: true });
    } catch (e) {
      notifyError((e as Error).message || 'Could not delete the briefing.');
    } finally {
      busy = false;
    }
  }

  async function performGroupDelete() {
    const target = confirmGroupDelete.target;
    confirmGroupDelete.cancel();
    if (!target || busy) return;
    busy = true;
    try {
      await deleteGroupBriefing(groupId, groupName, target.revision);
      briefingsRefreshNonce.update((n) => n + 1);
      await goto(`${base}/briefings/settings/`, { replaceState: true });
    } catch (e) {
      notifyError((e as Error).message || 'Could not delete the briefing.');
    } finally {
      busy = false;
    }
  }

  const menu = $derived.by((): KebabItem[] => {
    if (groupId && groupTarget?.capabilities.author_briefings) {
      const items: KebabItem[] = [
        {
          label: 'Duplicate to a group',
          onSelect: () => {
            const from = groupEditor?.duplicateSource();
            if (from) duplicating = { from, exclude: '' };
          },
        },
      ];
      // The server deletes only a disabled definition, so say so up front.
      const target = groupEditor?.deletion();
      if (target)
        items.push({
          label: target.enabled ? 'Delete (turn off first)' : 'Delete',
          danger: true,
          disabled: busy || target.enabled,
          onSelect: () => confirmGroupDelete.ask(target),
        });
      return items;
    }
    if (personalName && schedule?.id !== undefined) {
      const row = schedule;
      return [
        { label: 'Duplicate', disabled: busy, onSelect: () => void clonePersonal(row) },
        {
          label: 'Duplicate to a group',
          onSelect: () => (duplicating = { from: personalDuplicateSource(row), exclude: '' }),
        },
        { label: 'Delete', danger: true, onSelect: () => confirmDelete.ask(row) },
      ];
    }
    return [];
  });

  // --- Page header ---
  const title = $derived.by(() => {
    if (view === 'delivery') return 'Delivery';
    if (view === 'shared') return 'Shared blocks';
    if (creating) return 'New briefing';
    if (groupId) return groupHeading || derivedTitle(groupName);
    if (personalName) return schedule?.title || derivedTitle(personalName);
    return 'Briefings';
  });
  let groupHeading = $state('');
  const description = $derived.by(() => {
    if (view === 'delivery')
      return 'How briefings reach you, and which group briefings you receive.';
    if (view === 'shared')
      return 'Admin only. Content generated once on a schedule and read by any briefing through a Shared source.';
    if (creating) return undefined;
    if (groupId && groupTarget)
      return groupTarget.system_key === 'all_users'
        ? 'All users · generated once and sent to everyone on this installation'
        : `${groupTarget.label} · ${groupTarget.member_count ?? 0} members · generated once and sent to each member`;
    if (personalName)
      return `Just me${schedule ? ` · ${channelLabel(schedule.output)}${schedule.enabled ? '' : ' · Off'}` : ''}`;
    return undefined;
  });
</script>

<svelte:head>
  <title>Briefings settings</title>
</svelte:head>

<SettingsLayout {title} {description} {loading} {error}>
  {#snippet headerActions()}
    {#if menu.length}<KebabMenu items={menu} ariaLabel="Briefing actions" />{/if}
  {/snippet}
  {#if !moduleEnabled}
    <Alert appearance="inline" variant="info">
      Briefings module is disabled. Enable it in
      <a href="{base}/settings">Settings → Preferences</a> to manage schedules and content.
    </Alert>
  {:else if view === 'delivery'}
    <Card title="Email" description="For every briefing delivered to you by email.">
      <Field
        label="Send as HTML"
        checkbox
        badge={emailHtmlManaged ? MANAGED_BADGE : undefined}
        hint="Formatted mail, so an article link stays clickable. A plain-text copy is always sent alongside it."
      >
        <Checkbox bind:checked={emailHtml} disabled={emailHtmlManaged} />
      </Field>
    </Card>
    <GroupDeliveryPreferences />
  {:else if view === 'shared'}
    {#if isAdmin}<SharedBlocksSettings {browseOptions} />{/if}
  {:else if creating}
    <NewBriefing
      {targets}
      {channels}
      timezone={deploymentTimezone}
      {outputs}
      {rooms}
      initialGroup={params.get('group') ?? ''}
      taken={allNames}
    />
  {:else if groupId}
    {#key `${groupId}/${groupName}`}
      <GroupBriefingEditor
        bind:this={groupEditor}
        group={groupId}
        name={groupName}
        target={groupTarget}
        {channels}
        templates={config?.block_templates ?? []}
        {browseOptions}
        sharedBlockOptions={sharedBlockSelectOptions}
        onTitle={(value) => (groupHeading = value)}
      />
    {/key}
  {:else if personalName}
    {#key personalName}
      <PersonalBriefingEditor
        name={personalName}
        {schedule}
        blocks={personalBlocks}
        templates={config?.block_templates ?? []}
        {sourceKinds}
        {browseOptions}
        {rssOptions}
        sharedBlockOptions={sharedBlockSelectOptions}
        feedsAvailable={!!feedOptions?.available && rssOptions.length > 0}
        {emailAccounts}
        {outputs}
        {rooms}
        editBlockId={params.get('name') === personalName
          ? Number(params.get('block')) || null
          : null}
        onSaved={reloadSchedule}
        onBlocksChange={updateBlocks}
      />
    {/key}
  {:else if config}
    <EmptyState
      title="No briefings yet"
      actionLabel="New briefing"
      actionHref={briefingSettingsUrl({ kind: 'new' })}
    >
      A briefing gathers your sources on a schedule and delivers one summary.
    </EmptyState>
  {/if}
</SettingsLayout>

{#if duplicating}
  <DuplicateToGroupDialog
    from={duplicating.from}
    {targets}
    excludeGroup={duplicating.exclude}
    onClose={() => (duplicating = null)}
  />
{/if}

{#if confirmDelete.target}
  <ConfirmDialog
    bind:open={confirmDelete.open}
    title="Delete briefing"
    message={`Are you sure you want to delete ${confirmDelete.target.title || derivedTitle(confirmDelete.target.name)}? Its schedule and content blocks are removed. Past editions stay in your archive.`}
    confirmLabel="Delete"
    onConfirm={performDelete}
  />
{/if}

{#if confirmGroupDelete.target}
  <ConfirmDialog
    bind:open={confirmGroupDelete.open}
    title="Delete briefing"
    message={`Are you sure you want to delete ${confirmGroupDelete.target.title}? It stops for every member of ${groupTarget?.label ?? 'the group'}, and any unsaved changes are lost. Editions already published stay readable.`}
    confirmLabel="Delete"
    confirmVariant="danger"
    onConfirm={performGroupDelete}
  />
{/if}
