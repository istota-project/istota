<script lang="ts">
  import { untrack } from 'svelte';
  import { base } from '$app/paths';
  import {
    getGroup,
    getGroupBriefing,
    saveGroupBriefing,
    getBriefingAudience,
    getBriefingResources,
    approveBriefingSource,
    revokeBriefingSource,
    runGroupBriefing,
    getGroupBriefingRun,
    getGroupBriefingRuns,
    getGroupBriefingDelivery,
    retryGroupBriefingDelivery,
    type BriefingTarget,
    type GroupBriefingDefinition,
    type GroupBriefingPayload,
    type GroupBriefingGeneration,
    type BriefingAudience,
    type BriefingSourceApproval,
    type BriefingDelivery,
    type GroupBriefingRun,
    type BriefingBlock,
    type BriefingBlockTemplate,
  } from '$lib/api';
  import {
    Badge,
    Button,
    ConfirmDialog,
    EmptyState,
    Input,
    KebabMenu,
    Modal,
    MultiSelect,
    NoticeBanner,
    OptionCards,
    PageLoading,
    Select,
    type KebabItem,
    type SelectOption,
  } from '$lib/components/ui';
  import { SettingsCard, SettingsField } from '$lib/components/settings';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';
  import { briefingsRefreshNonce } from '$lib/stores/briefings';
  import { notifyError, notifySuccess } from '$lib/stores/notices';
  import { getCurrentUser } from '$lib/userContext';
  import { formatDateTime } from '$lib/dateFormat';
  import BlockBuilder from './BlockBuilder.svelte';
  import BriefingEdition from './BriefingEdition.svelte';
  import SourceApprovalDialog from './settings/SourceApprovalDialog.svelte';
  import type { DuplicateSource } from './settings/DuplicateToGroupDialog.svelte';
  import {
    calendarDisclosure,
    channelLabel,
    contributionLabel,
    derivedTitle,
    GROUP_SOURCE_KINDS,
  } from './settings/briefingSettings';

  let {
    group,
    name,
    target,
    channels,
    templates = [],
    browseOptions = [],
    sharedBlockOptions = [],
    onTitle,
  }: {
    group: string;
    name: string;
    target: BriefingTarget | undefined;
    channels: string[];
    templates?: BriefingBlockTemplate[];
    browseOptions?: SelectOption[];
    sharedBlockOptions?: SelectOption[];
    /** The saved title, for the page heading. */
    onTitle?: (title: string) => void;
  } = $props();

  const identity = getCurrentUser();
  const me = identity.user.username;

  let saved = $state<GroupBriefingDefinition | null>(null);
  let loading = $state(true);
  let accessLost = $state(false);
  let busy = $state(false);

  let title = $state('');
  let cron = $state('');
  let timezone = $state('');
  let channel = $state('in_app');
  let enabled = $state(false);
  let excludedUsers = $state<string[]>([]);
  let memberOptions = $state<SelectOption[]>([]);
  let mode = $state<'prompt' | 'blocks'>('prompt');
  let prompt = $state('');
  let researchEnabled = $state(false);
  let researchPrompt = $state('');
  let personalInformation = $state(false);
  let acknowledged = $state(false);
  let blocks = $state<BriefingBlock[]>([]);
  let sourceKeys = $state<Record<number, string>>({});
  let draftId = 0;
  let savedPayload = $state('');

  let audience = $state<BriefingAudience | null>(null);
  let resources = $state<{
    files: string[];
    calendars: SelectOption[];
    calendar_unavailable: boolean;
  }>({ files: [], calendars: [], calendar_unavailable: false });
  let reviewing = $state<BriefingSourceApproval | null>(null);

  let runs = $state<GroupBriefingRun[]>([]);
  let runsTotal = $state(0);
  let deliveries = $state<Record<number, BriefingDelivery>>({});
  let viewing = $state<GroupBriefingRun | null>(null);
  let retrying = $state<GroupBriefingRun | null>(null);
  let pending = $state<GroupBriefingRun | null>(null);

  const author = $derived(!accessLost && !!saved?.capabilities.author_briefings);
  // A former owner keeps their draft on screen, locked, rather than losing it.
  const showsDefinition = $derived(!!saved?.capabilities.author_briefings);
  const canGenerate = $derived(author && saved?.capabilities.generate_briefings !== false);
  const held = $derived(!!saved?.held);
  const allUsers = $derived(target?.system_key === 'all_users');
  const groupLabel = $derived(target?.label ?? group);
  const SOURCE_KINDS = GROUP_SOURCE_KINDS;

  const payload = $derived.by((): GroupBriefingPayload => {
    const shaped = blocks.map((b) => ({
      title: b.title,
      directive: b.directive || null,
      render_mode: b.render_mode,
      options: b.options,
      sources: b.sources.map((s) => ({ id: sourceKeys[s.id], kind: s.kind, config: s.config })),
    }));
    return {
      name,
      title,
      cron,
      timezone,
      channel,
      excluded_user_ids: excludedUsers,
      revision: saved?.revision ?? 0,
      enabled,
      generation_mode: mode,
      generation: {
        version: 1,
        contains_personal_information: personalInformation,
        audience_acknowledged: personalInformation && acknowledged,
        ...(mode === 'prompt'
          ? {
              prompt,
              research: { enabled: researchEnabled, prompt: researchPrompt },
              inputs: shaped.flatMap((b) => b.sources),
            }
          : { blocks: shaped }),
      },
    };
  });
  const dirty = $derived(!!savedPayload && JSON.stringify(payload) !== savedPayload);
  const unsupported = $derived(
    blocks.flatMap((b) => b.sources).filter((s) => !SOURCE_KINDS.includes(s.kind)),
  );
  const switchedOff = $derived(blocks.flatMap((b) => b.sources).filter((s) => s.enabled === false));
  const awaiting = $derived(audience?.sources.filter((s) => !s.approved) ?? []);
  // What stops this briefing running, in the order an owner would fix it.
  const blocker = $derived.by(() => {
    if (unsupported.length) return 'Remove the sources a group briefing cannot use.';
    if (switchedOff.length) return 'Turn on or remove the switched-off sources.';
    if (mode === 'prompt' && !prompt.trim()) return 'Add instructions for each edition.';
    if (mode === 'prompt' && researchEnabled && !researchPrompt.trim())
      return 'Add instructions for public research, or turn it off.';
    if (mode === 'blocks' && !blocks.some((b) => b.sources.length))
      return 'Add at least one block with a source.';
    if (awaiting.length) return 'Every shared personal source needs its owner’s approval.';
    if (personalInformation && !acknowledged)
      return 'Confirm the instructions may be shared with this audience.';
    return '';
  });
  const runBlocker = $derived(dirty ? 'Save your changes first.' : blocker);

  function localBlock(blockTitle: string): BriefingBlock {
    return {
      id: ++draftId,
      briefing_name: name,
      position: blocks.length,
      title: blockTitle,
      directive: '',
      render_mode: 'synthesis',
      options: {},
      sources: [],
    };
  }
  function importBlocks(generation: GroupBriefingGeneration) {
    sourceKeys = {};
    const source = generation.blocks ?? [
      {
        title: 'Sources',
        directive: '',
        render_mode: 'synthesis',
        options: {},
        sources: generation.inputs ?? [],
      },
    ];
    blocks = source.map((b, position) => ({
      ...localBlock(b.title),
      ...b,
      directive: b.directive ?? '',
      position,
      sources: b.sources.map((s, index) => {
        const id = ++draftId;
        sourceKeys[id] = s.id;
        return { ...s, id, position: index, enabled: true };
      }),
    }));
  }
  function updateBlocks(_name: string, next: BriefingBlock[]) {
    for (const block of next)
      for (const source of block.sources)
        if (!sourceKeys[source.id]) sourceKeys[source.id] = `source-${crypto.randomUUID()}`;
    blocks = next;
  }

  // A refusal of access is a lasting state, shown in place; anything else is
  // feedback about the action that just failed.
  function fail(e: unknown) {
    const status = (e as { status?: number })?.status;
    if (status === 403 || status === 404) accessLost = true;
    else notifyError((e as Error)?.message || 'That did not work. Try again.');
  }

  async function load() {
    loading = true;
    try {
      const fresh = await getGroupBriefing(group, name);
      saved = fresh;
      onTitle?.(fresh.title || derivedTitle(name));
      title = fresh.title;
      cron = fresh.cron;
      timezone = fresh.timezone;
      channel = fresh.channel;
      enabled = fresh.enabled;
      excludedUsers = fresh.excluded_user_ids ?? [];
      mode = fresh.generation_mode ?? 'prompt';
      const generation = fresh.generation ?? { version: 1 };
      prompt = generation.prompt ?? '';
      researchEnabled = generation.research?.enabled ?? false;
      researchPrompt = generation.research?.prompt ?? '';
      personalInformation = generation.contains_personal_information ?? false;
      acknowledged = generation.audience_acknowledged ?? false;
      importBlocks(generation);
      savedPayload = JSON.stringify(payload);
      const author = fresh.capabilities.author_briefings;
      const [aud, res, members, history] = await Promise.all([
        author ? getBriefingAudience(group, name) : Promise.resolve(null),
        getBriefingResources(group).catch(() => resources),
        author ? getGroup(group) : Promise.resolve(null),
        author ? getGroupBriefingRuns(group, name) : Promise.resolve(null),
      ]);
      audience = aud ?? {
        member_count: target?.member_count ?? 0,
        deliverable: 0,
        excluded: 0,
        muted: 0,
        missing_destination: 0,
        sources: fresh.source_approvals ?? [],
      };
      resources = res;
      if (members)
        memberOptions = members.members.map((m) => ({
          value: m.user_id,
          label: m.display_name || m.user_id,
        }));
      if (history) {
        runs = history.items;
        runsTotal = history.total;
        await loadDeliveries();
      }
    } catch (e) {
      fail(e);
    } finally {
      loading = false;
    }
  }

  async function loadDeliveries() {
    const published = runs
      .filter((r) => r.mode === 'publish' && r.state === 'published')
      .slice(0, 5);
    const results = await Promise.allSettled(
      published.map((r) => getGroupBriefingDelivery(group, name, r.id)),
    );
    deliveries = Object.fromEntries(
      published.flatMap((r, i) => {
        const result = results[i];
        return result.status === 'fulfilled' ? [[r.id, result.value]] : [];
      }),
    );
  }

  async function save() {
    busy = true;
    try {
      const result = await saveGroupBriefing(group, payload, name);
      saved = result;
      onTitle?.(result.title || derivedTitle(name));
      savedPayload = JSON.stringify(payload);
      audience = await getBriefingAudience(group, name);
      briefingsRefreshNonce.update((n) => n + 1);
    } catch (e) {
      fail(e);
    } finally {
      busy = false;
    }
  }

  useSettingsSave(() => (author ? { dirty, saving: busy, save } : null));

  async function approve(resource: Record<string, unknown>) {
    if (!reviewing) return;
    busy = true;
    try {
      await approveBriefingSource(group, name, reviewing, resource);
      reviewing = null;
      await load();
    } catch (e) {
      fail(e);
    } finally {
      busy = false;
    }
  }
  async function revoke() {
    if (!reviewing) return;
    busy = true;
    try {
      await revokeBriefingSource(group, name, reviewing);
      reviewing = null;
      await load();
    } catch (e) {
      fail(e);
    } finally {
      busy = false;
    }
  }

  async function run(kind: 'preview' | 'publish') {
    busy = true;
    try {
      const created = await runGroupBriefing(group, name, kind, crypto.randomUUID());
      pending = created;
      runs = [created, ...runs];
      runsTotal += 1;
    } catch (e) {
      fail(e);
    } finally {
      busy = false;
    }
  }

  // Follow a run the owner just started until it settles.
  $effect(() => {
    const current = pending;
    if (!current) return;
    let active = true;
    const timer = setTimeout(async () => {
      try {
        const next = await getGroupBriefingRun(group, name, current.id);
        if (!active) return;
        runs = runs.map((r) => (r.id === next.id ? next : r));
        if (['queued', 'researching', 'generating'].includes(next.state)) pending = next;
        else {
          pending = null;
          if (next.state === 'preview_ready') viewing = next;
          if (next.state === 'published') await loadDeliveries();
        }
      } catch (e) {
        if (active) {
          pending = null;
          fail(e);
        }
      }
    }, 1500);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  });

  async function retry() {
    const target = retrying;
    retrying = null;
    if (!target) return;
    busy = true;
    try {
      const result = await retryGroupBriefingDelivery(
        group,
        name,
        target.id,
        !!deliveries[target.id]?.counts.uncertain,
      );
      notifySuccess(`${result.retried} deliveries queued from the saved edition.`);
      await loadDeliveries();
    } catch (e) {
      fail(e);
    } finally {
      busy = false;
    }
  }

  async function openRun(r: GroupBriefingRun) {
    try {
      viewing = await getGroupBriefingRun(group, name, r.id);
    } catch (e) {
      fail(e);
    }
  }

  const STATE_LABELS: Record<string, string> = {
    queued: 'Queued',
    researching: 'Researching',
    generating: 'Generating',
    preview_ready: 'Preview',
    published: 'Published',
    failed: 'Failed',
    superseded: 'Superseded',
    cancelled: 'Cancelled',
  };
  const STATE_VARIANTS: Record<string, 'neutral' | 'danger' | 'success' | 'info' | 'warn'> = {
    queued: 'info',
    researching: 'info',
    generating: 'info',
    preview_ready: 'neutral',
    published: 'success',
    failed: 'danger',
    superseded: 'neutral',
    cancelled: 'neutral',
  };
  function deliverySummary(r: GroupBriefingRun): string {
    if (r.mode === 'preview') return 'Private preview, sent to nobody';
    if (r.state === 'failed') return r.error_code ? r.error_code.replaceAll('_', ' ') : '';
    const d = deliveries[r.id];
    if (!d) return '';
    return Object.entries(d.counts)
      .map(([state, count]) => `${count} ${state}`)
      .join(' · ');
  }
  function runMenu(r: GroupBriefingRun): KebabItem[] {
    const items: KebabItem[] = [];
    if (r.state === 'published' && r.mode === 'publish')
      items.push({
        label: 'Read',
        href: `${base}/briefings/?group=${encodeURIComponent(group)}&briefing=${encodeURIComponent(name)}&run=${r.id}`,
      });
    if (r.state === 'preview_ready')
      items.push({ label: 'View preview', onSelect: () => openRun(r) });
    const d = deliveries[r.id];
    if (d && (d.counts.failed || d.counts.uncertain))
      items.push({ label: 'Retry failed deliveries', onSelect: () => (retrying = r) });
    return items;
  }
  function approvalStatus(a: BriefingSourceApproval): {
    label: string;
    variant: 'success' | 'warn' | 'neutral';
  } {
    if (a.approved) return { label: 'Approved', variant: 'success' };
    if (a.owner_id === me) return { label: 'Needs your approval', variant: 'warn' };
    const owner = memberOptions.find((m) => m.value === a.owner_id)?.label ?? a.owner_id;
    return { label: `Waiting for ${owner}`, variant: 'neutral' };
  }
  function ownerName(id: string): string {
    if (id === me) return 'Your';
    const label = memberOptions.find((m) => m.value === id)?.label ?? id;
    return `${label}’s`;
  }

  // What the page's Delete acts on: the saved definition, not unsaved edits.
  export function deletion(): { revision: number; title: string; enabled: boolean } | null {
    if (!author || !saved || saved.deleted) return null;
    return {
      revision: saved.revision,
      title: saved.title || derivedTitle(name),
      enabled: saved.enabled,
    };
  }

  export function duplicateSource(): DuplicateSource {
    return {
      name,
      title,
      cron,
      timezone,
      channel,
      excluded_user_ids: excludedUsers,
      mode,
      prompt,
      research: { enabled: researchEnabled, prompt: researchPrompt },
      containsPersonalInformation: personalInformation,
      blocks: blocks.map((b) => ({
        title: b.title,
        directive: b.directive || null,
        render_mode: b.render_mode,
        options: b.options,
        sources: b.sources.map((s) => ({ kind: s.kind, config: s.config, enabled: s.enabled })),
      })),
    };
  }

  const templatesForGroup = $derived([
    ...templates.filter((t) => t.block.sources.every((s) => SOURCE_KINDS.includes(s.kind))),
    {
      id: 'group-notebook',
      label: 'Group notebook',
      description: "This group's notebook.",
      block: {
        title: 'Group notebook',
        directive: '',
        render_mode: 'structured',
        options: {},
        sources: [{ kind: 'group_memory', config: {} }],
      },
    },
  ]);

  // Mounted per briefing (the page keys it), so this loads once.
  $effect(() => {
    untrack(() => void load());
  });
</script>

{#if loading && !saved}
  <PageLoading />
{:else if !saved}
  <p class="center-msg error">
    {accessLost ? 'This briefing is not available to you.' : 'Could not load this briefing.'}
  </p>
{:else}
  {#if accessLost}
    <NoticeBanner title="You can no longer edit this briefing" variant="danger" collapsed={false}>
      Its group, or your role in the group, has changed. Reload to see what you can still do.
    </NoticeBanner>
  {/if}

  {#if showsDefinition}
    <SettingsCard title="Schedule and delivery">
      {#if held}
        <NoticeBanner title="Paused by an administrator" variant="warn" collapsed={false}>
          This briefing will not run until an administrator resumes it.
        </NoticeBanner>
      {/if}
      <fieldset class="fields" disabled={busy || accessLost}>
        <SettingsField label="Title" hint="Shown on every edition, followed by its date.">
          <Input bind:value={title} placeholder={derivedTitle(name)} />
        </SettingsField>
        <div class="field-row">
          <SettingsField
            label="Schedule"
            hint="A cron expression, for example 0 7 * * 1-5 for weekdays at 07:00."
          >
            <Input bind:value={cron} monospace />
          </SettingsField>
          <SettingsField label="Timezone" hint="One schedule for everyone in the group.">
            <Input bind:value={timezone} />
          </SettingsField>
          <SettingsField
            label="Delivery"
            labelled={false}
            hint="Each member receives it at their own address for this channel. Every edition is also readable in Briefings."
          >
            <Select
              value={channel}
              options={channels.map((c) => ({ value: c, label: channelLabel(c) }))}
              onValueChange={(v) => (channel = v)}
              ariaLabel="Delivery"
              fullWidth
            />
          </SettingsField>
        </div>
        <SettingsField
          label="Excluded members"
          labelled={false}
          hint="Excluded members get no copies or notifications of this briefing, but can still read its editions."
        >
          <MultiSelect
            value={excludedUsers}
            options={[
              ...memberOptions,
              ...excludedUsers
                .filter((id) => !memberOptions.some((m) => m.value === id))
                .map((id) => ({ value: id, label: `${id} (left the group)` })),
            ]}
            onValueChange={(next) => (excludedUsers = next)}
            placeholder="Search members…"
            emptyLabel="Nobody excluded."
            ariaLabel="Excluded members"
          />
        </SettingsField>
        <SettingsField
          label="Enabled"
          checkbox
          hint="When on, each scheduled run publishes an edition and sends it to members."
          warning={enabled && blocker ? blocker : undefined}
        >
          <input type="checkbox" bind:checked={enabled} disabled={held} />
        </SettingsField>
      </fieldset>
    </SettingsCard>

    <SettingsCard title="Content">
      <fieldset class="fields" disabled={busy || accessLost}>
        <OptionCards
          options={[
            {
              value: 'prompt',
              title: 'Write instructions',
              description: 'The model writes each edition from your instructions and any sources.',
            },
            {
              value: 'blocks',
              title: 'Build from blocks',
              description: 'Ordered sections, each from its own sources, as in personal briefings.',
            },
          ]}
          bind:value={mode}
          label="How the content is made"
        />
        {#if mode === 'prompt'}
          <SettingsField
            label="Instructions"
            wide
            hint="Used to write each edition. Never sent to public research."
          >
            <textarea rows="4" bind:value={prompt}></textarea>
          </SettingsField>
          <SettingsField
            label="Research the web first"
            checkbox
            hint="A separate step reads public pages before the edition is written. It never sees the group's notebook, files or anyone's personal sources."
          >
            <input type="checkbox" bind:checked={researchEnabled} />
          </SettingsField>
          {#if researchEnabled}
            <SettingsField
              label="What to research"
              wide
              hint="Sent to the research step, so keep anything private out of it."
            >
              <textarea rows="3" bind:value={researchPrompt}></textarea>
            </SettingsField>
          {/if}
          <p class="micro-label">Sources (optional)</p>
        {/if}
        {#if unsupported.length}
          <NoticeBanner title="Some sources cannot be used by a group" collapsed={false}>
            Remove {unsupported.map((s) => s.kind).join(', ')}, or replace them with a personal file
            or calendar that its owner shares.
          </NoticeBanner>
        {/if}
        {#if switchedOff.length}
          <NoticeBanner title="Some sources are switched off" collapsed={false}>
            Group briefings have no switched-off sources. Turn each one on or remove it.
          </NoticeBanner>
        {/if}
        {#key `${group}/${name}`}
          <BlockBuilder
            briefingName={name}
            {blocks}
            templates={templatesForGroup}
            sourceKinds={SOURCE_KINDS}
            {browseOptions}
            rssOptions={[]}
            {sharedBlockOptions}
            feedsAvailable={false}
            localDraft
            allowSourceDisable={false}
            userId={me}
            fileOptions={resources.files}
            calendarOptions={resources.calendars}
            onChange={updateBlocks}
          />
        {/key}
      </fieldset>
    </SettingsCard>
  {/if}

  <SettingsCard title={author ? 'Audience and sharing' : 'Your shared sources'}>
    {#if author && audience}
      <p class="summary">
        {#if channel === 'in_app'}
          {audience.member_count - audience.excluded} of {audience.member_count} members are notified
          in Briefings{#if audience.excluded}, {audience.excluded} excluded{/if}.
        {:else}
          {audience.deliverable} of {audience.member_count} members get it by {channelLabel(
            channel,
          ).toLowerCase()}{#if audience.excluded}, {audience.excluded} excluded{/if}{#if audience.muted},
            {audience.muted} muted{/if}{#if audience.missing_destination},
            {audience.missing_destination} without an address{/if}.
        {/if}
        <span class="caption">
          {allUsers
            ? 'Everyone on this installation, including people who join later, can read past editions.'
            : 'Current and future members can read past editions.'}
        </span>
      </p>
      <SettingsField
        label="The instructions include personal information"
        checkbox
        hint="Instructions are shared with the same audience as the sources. Nothing detects personal information for you."
      >
        <input type="checkbox" bind:checked={personalInformation} disabled={busy} />
      </SettingsField>
      {#if personalInformation}
        <SettingsField
          label="I agree to share them with everyone who can read this briefing, now and later"
          checkbox
        >
          <input type="checkbox" bind:checked={acknowledged} disabled={busy} />
        </SettingsField>
      {/if}
    {:else if !author}
      <p class="caption">
        Only the group's owners can change this briefing. You can review the sources you share with
        it.
      </p>
    {/if}
    {#if audience?.sources.length}
      <ul class="sources">
        {#each audience.sources.filter((a) => author || a.owner_id === me) as approval (approval.source_id)}
          {@const status = approvalStatus(approval)}
          <li>
            <span class="source-text">
              <span
                >{ownerName(approval.owner_id)}
                {approval.resource.kind === 'personal_calendar' ? 'calendar' : 'file'} · {contributionLabel(
                  approval.resource,
                  resources.calendars,
                )}</span
              >
              <span class="caption"
                >{approval.resource.kind === 'personal_calendar'
                  ? calendarDisclosure(approval.resource.config)
                  : 'Whole file'}</span
              >
            </span>
            <Badge variant={status.variant} size="sm">{status.label}</Badge>
            {#if approval.owner_id === me && !accessLost}
              <Button size="sm" onclick={() => (reviewing = approval)}
                >{approval.approved ? 'Manage' : 'Review'}</Button
              >
            {/if}
          </li>
        {/each}
      </ul>
    {:else if author}
      <p class="caption">No personal sources. Nothing private is shared with this group.</p>
    {/if}
    {#if author && dirty}
      <p class="caption">Save your changes to update these numbers and approvals.</p>
    {/if}
  </SettingsCard>

  {#if author}
    <SettingsCard title="Editions">
      {#snippet actions()}
        <Button
          size="sm"
          variant="secondary"
          onclick={() => run('preview')}
          disabled={!canGenerate || !!runBlocker || busy || !!pending}>Preview</Button
        >
        <Button
          size="sm"
          onclick={() => run('publish')}
          disabled={!canGenerate || !!runBlocker || busy || !!pending}>Generate now</Button
        >
      {/snippet}
      <p class="caption">
        {runBlocker ||
          'Preview is private and sends nothing. Generate now publishes a new edition to members.'}
      </p>
      {#if runs.length}
        <ul class="runs">
          {#each runs as r (r.id)}
            <li>
              <span class="run-date"
                >{formatDateTime(r.published_at || r.created_at || '', {
                  dateStyle: 'medium',
                  timeStyle: 'short',
                })}</span
              >
              <Badge variant={STATE_VARIANTS[r.state] ?? 'neutral'} size="sm"
                >{r.mode === 'preview' && r.state === 'preview_ready'
                  ? 'Preview'
                  : (STATE_LABELS[r.state] ?? r.state)}</Badge
              >
              <span class="caption run-summary">{deliverySummary(r)}</span>
              {#if runMenu(r).length}
                <KebabMenu items={runMenu(r)} ariaLabel="Edition actions" />
              {:else}<span class="kebab-spacer"></span>{/if}
            </li>
          {/each}
        </ul>
        {#if runs.length < runsTotal}<p class="caption">
            Showing the latest {runs.length} of {runsTotal}.
          </p>{/if}
      {:else}
        <EmptyState size="sm">No editions yet.</EmptyState>
      {/if}
    </SettingsCard>
  {/if}
{/if}

{#if reviewing}
  <SourceApprovalDialog
    approval={reviewing}
    userId={me}
    {groupLabel}
    memberCount={audience?.member_count ?? target?.member_count ?? 0}
    {allUsers}
    {channel}
    files={resources.files}
    calendars={resources.calendars}
    {busy}
    onApprove={approve}
    onRevoke={revoke}
    onClose={() => (reviewing = null)}
  />
{/if}

{#if viewing}
  <Modal
    open={true}
    variant="viewer"
    title={viewing.mode === 'preview' ? 'Preview' : (viewing.subject ?? title)}
    onOpenChange={(open) => {
      if (!open) viewing = null;
    }}
  >
    <BriefingEdition
      showMetadata={false}
      edition={{
        id: viewing.id,
        briefing_name: name,
        subject: viewing.subject ?? title,
        body_md: viewing.body_md,
        document: viewing.document ?? null,
        read_at: null,
        generated_at: viewing.published_at || viewing.created_at || '',
        task_id: null,
        delivered_to: [],
      }}
    />
  </Modal>
{/if}

{#if retrying}
  <ConfirmDialog
    open={true}
    title="Retry failed deliveries"
    message={deliveries[retrying.id]?.counts.uncertain
      ? 'Are you sure you want to resend this edition where delivery failed or is uncertain? An uncertain delivery may already have arrived, so those members could get it twice.'
      : 'Are you sure you want to resend this edition where delivery failed? Nothing is generated again.'}
    confirmLabel="Retry"
    confirmVariant="primary"
    onConfirm={retry}
    onCancel={() => (retrying = null)}
  />
{/if}

<style>
  .fields {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    border: 0;
    padding: 0;
    margin: 0;
    min-width: 0;
  }
  .field-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(12rem, 100%), 1fr));
    gap: var(--space-3);
    align-items: start;
  }
  .micro-label {
    margin: var(--space-2) 0 0;
  }
  .summary {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    margin: 0 0 var(--space-3);
  }
  .sources,
  .runs {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .sources li,
  .runs li {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding-block: var(--space-2);
    border-bottom: 1px solid var(--border-subtle);
  }
  .sources li:last-child,
  .runs li:last-child {
    border-bottom: none;
  }
  .source-text {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .run-date {
    flex-shrink: 0;
    min-width: 10rem;
  }
  .run-summary {
    flex: 1;
    min-width: 0;
  }
  .kebab-spacer {
    width: var(--control-height-sm);
    flex-shrink: 0;
  }
</style>
