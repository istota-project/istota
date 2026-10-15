<script lang="ts">
  import { untrack } from 'svelte';
  import { page } from '$app/state';
  import {
    getBriefingTargets,
    getGroup,
    getGroupBriefings,
    getGroupBriefing,
    saveGroupBriefing,
    getBriefingAudience,
    getBriefingResources,
    approveBriefingSource,
    revokeBriefingSource,
    runGroupBriefing,
    getGroupBriefingRun,
    type BriefingTarget,
    type GroupBriefingDefinition,
    type GroupBriefingPayload,
    type GroupBriefingGeneration,
    type BriefingAudience,
    type BriefingSourceApproval,
    type GroupBriefingRun,
    type BriefingBlock,
    type BriefingBlockTemplate,
    type UserBriefingRow,
  } from '$lib/api';
  import { Button, Input, Select, MultiSelect, type SelectOption } from '$lib/components/ui';
  import { SettingsCard, SettingsField } from '$lib/components/settings';
  import { briefingsRefreshNonce } from '$lib/stores/briefings';
  import { getCurrentUser } from '$lib/userContext';
  import BlockBuilder from './BlockBuilder.svelte';
  import BriefingEdition from './BriefingEdition.svelte';
  import SourceConfigFields from './SourceConfigFields.svelte';
  let {
    templates = [],
    browseOptions = [],
    sharedBlockOptions = [],
    copy = null,
    onGroupActive,
    onPersonalNew,
  }: {
    templates?: BriefingBlockTemplate[];
    browseOptions?: SelectOption[];
    sharedBlockOptions?: SelectOption[];
    copy?: { briefing: UserBriefingRow; blocks: BriefingBlock[] } | null;
    onGroupActive?: (active: boolean) => void;
    onPersonalNew?: () => void;
  } = $props();
  const identity = getCurrentUser();
  let targets = $state<BriefingTarget[]>([]);
  let channels = $state<string[]>([]);
  let deploymentTimezone = $state('UTC');
  let sourceScopes = $state<Record<string, string>>({});
  let groupDefinitions = $state<GroupBriefingDefinition[]>([]);
  let editorOpen = $state(false);
  let targetId = $state('');
  let groupSaved = $state<GroupBriefingDefinition | null>(null);
  let groupTitle = $state('');
  let groupName = $state('');
  let groupCron = $state('0 7 * * *');
  let groupTimezone = $state('UTC');
  let groupChannel = $state('in_app');
  let excludedUsers = $state<string[]>([]);
  let memberOptions = $state<SelectOption[]>([]);
  let groupMode = $state<'prompt' | 'blocks'>('prompt');
  let generationPrompt = $state('');
  let researchEnabled = $state(false);
  let researchPrompt = $state('');
  let personalInformation = $state(false);
  let audienceAcknowledged = $state(false);
  let groupBlocks = $state<BriefingBlock[]>([]);
  let sourceKeys = $state<Record<number, string>>({});
  let draftId = 0;
  let savedPayload = $state('');
  let audience = $state<BriefingAudience | null>(null);
  let checkedApprovals = $state<Record<string, boolean>>({});
  let contributionDrafts = $state<Record<string, Record<string, unknown>>>({});
  let resourceOptions = $state<{
    files: string[];
    calendars: SelectOption[];
    calendar_unavailable: boolean;
  }>({ files: [], calendars: [], calendar_unavailable: false });
  let groupError = $state('');
  let groupBusy = $state(false);
  let groupAccessLost = $state(false);
  let preview = $state<GroupBriefingRun | null>(null);
  const channelLabels: Record<string, string> = {
    in_app: 'In app',
    email: 'Email',
    talk: 'Talk',
    ntfy: 'ntfy',
  };
  const groupTarget = $derived(targets.find((t) => t.group_id === targetId));
  const groupEditing = $derived(editorOpen && !!targetId);
  const groupAuthor = $derived(
    !groupAccessLost &&
      !!groupTarget?.capabilities.author_briefings &&
      (!groupSaved || groupSaved.capabilities.author_briefings),
  );
  const groupSourceKinds = [
    'browse',
    'markets',
    'group_memory',
    'group_file',
    'shared_block',
    'personal_file',
    'personal_calendar',
  ];
  const groupPayload = $derived.by((): GroupBriefingPayload => {
    const blocks = groupBlocks.map((b) => ({
      title: b.title,
      directive: b.directive || null,
      render_mode: b.render_mode,
      options: b.options,
      sources: b.sources
        .filter((s) => s.enabled)
        .map((s) => ({ id: sourceKeys[s.id], kind: s.kind, config: s.config })),
    }));
    return {
      name: groupName,
      title: groupTitle,
      cron: groupCron,
      timezone: groupTimezone,
      channel: groupChannel,
      excluded_user_ids: excludedUsers,
      revision: groupSaved?.revision ?? 0,
      enabled: false,
      generation_mode: groupMode,
      generation: {
        version: 1,
        contains_personal_information: personalInformation,
        audience_acknowledged: audienceAcknowledged,
        ...(groupMode === 'prompt'
          ? {
              prompt: generationPrompt,
              research: { enabled: researchEnabled, prompt: researchPrompt },
              inputs: blocks.flatMap((b) => b.sources),
            }
          : { blocks }),
      },
    };
  });
  const groupDirty = $derived(JSON.stringify(groupPayload) !== savedPayload);
  const groupReady = $derived(
    !!groupSaved &&
      !groupDirty &&
      !!audience &&
      audience.sources.every((s) => s.approved) &&
      (!personalInformation || audienceAcknowledged),
  );
  const groupInvalidSources = $derived(
    groupBlocks.flatMap((b) => b.sources).filter((s) => !groupSourceKinds.includes(s.kind)),
  );

  function localBlock(title: string): BriefingBlock {
    return {
      id: ++draftId,
      briefing_name: groupName,
      position: groupBlocks.length,
      title,
      directive: '',
      render_mode: 'synthesis',
      options: {},
      sources: [],
    };
  }
  function importGroupBlocks(generation: GroupBriefingGeneration) {
    sourceKeys = {};
    groupBlocks = (
      generation.blocks ?? [
        {
          title: 'Selected inputs',
          directive: '',
          render_mode: 'synthesis',
          options: {},
          sources: generation.inputs ?? [],
        },
      ]
    ).map((b, position) => ({
      ...localBlock(b.title),
      ...b,
      directive: b.directive ?? '',
      position,
      sources: b.sources.map((s, position) => {
        const id = ++draftId;
        sourceKeys[id] = s.id;
        return { ...s, id, position, enabled: true };
      }),
    }));
  }
  function startNew() {
    editorOpen = true;
    onPersonalNew?.();
    targetId = '';
    groupSaved = null;
    groupName = '';
    groupTitle = '';
    groupCron = '0 7 * * *';
    groupTimezone = deploymentTimezone;
    groupChannel = 'in_app';
    excludedUsers = [];
    memberOptions = [];
    groupMode = 'prompt';
    generationPrompt = '';
    researchEnabled = false;
    researchPrompt = '';
    personalInformation = false;
    audienceAcknowledged = false;
    audience = null;
    checkedApprovals = {};
    contributionDrafts = {};
    groupError = '';
    preview = null;
    groupAccessLost = false;
    importGroupBlocks({ version: 1, inputs: [] });
  }
  async function selectTarget(value: string) {
    if (groupSaved) return;
    targetId = value;
    memberOptions = [];
    audience = null;
    checkedApprovals = {};
    contributionDrafts = {};
    audienceAcknowledged = false;
    preview = null;
    if (value) {
      groupBusy = true;
      try {
        const group = await getGroup(value);
        if (targetId !== value) return;
        memberOptions = group.members.map((m) => ({
          value: m.user_id,
          label: m.display_name || m.user_id,
        }));
        excludedUsers = excludedUsers.filter((id) => group.members.some((m) => m.user_id === id));
        resourceOptions = await getBriefingResources(value);
      } catch (e) {
        if (targetId === value) handleGroupError(e);
      } finally {
        if (targetId === value) groupBusy = false;
      }
    } else {
      excludedUsers = [];
    }
  }
  async function refreshGroups() {
    const discovery = await getBriefingTargets();
    targets = discovery.targets;
    channels = discovery.channels;
    deploymentTimezone = discovery.timezone;
    sourceScopes = discovery.source_scopes;
    const results = await Promise.all(
      targets.filter((t) => t.scope === 'group').map((t) => getGroupBriefings(t.group_id!)),
    );
    groupDefinitions = results.flatMap((r) => r.items);
  }
  async function openGroup(definition: GroupBriefingDefinition) {
    groupBusy = true;
    groupError = '';
    try {
      const fresh = await getGroupBriefing(definition.group_id, definition.name);
      startNew();
      targetId = fresh.group_id;
      groupSaved = fresh;
      groupName = fresh.name;
      groupTitle = fresh.title;
      groupCron = fresh.cron;
      groupTimezone = fresh.timezone;
      groupChannel = fresh.channel;
      excludedUsers = fresh.excluded_user_ids ?? [];
      groupMode = fresh.generation_mode ?? 'prompt';
      const generation = fresh.generation ?? { version: 1 };
      generationPrompt = generation.prompt ?? '';
      researchEnabled = generation.research?.enabled ?? false;
      researchPrompt = generation.research?.prompt ?? '';
      personalInformation = generation.contains_personal_information ?? false;
      audienceAcknowledged = generation.audience_acknowledged ?? false;
      importGroupBlocks(generation);
      savedPayload = JSON.stringify(groupPayload);
      audience = fresh.capabilities.author_briefings
        ? await getBriefingAudience(targetId, groupName)
        : {
            member_count: groupTarget?.member_count ?? 0,
            deliverable: 0,
            excluded: 0,
            muted: 0,
            missing_destination: 0,
            sources: fresh.source_approvals ?? [],
          };
      if (fresh.capabilities.author_briefings) {
        const group = await getGroup(targetId);
        memberOptions = group.members.map((m) => ({
          value: m.user_id,
          label: m.display_name || m.user_id,
        }));
      }
      resourceOptions = await getBriefingResources(targetId);
    } catch (e) {
      handleGroupError(e);
    } finally {
      groupBusy = false;
    }
  }
  function handleGroupError(e: unknown) {
    groupError = (e as Error).message || 'Briefing request failed. Reload before trying again.';
    const status = (e as { status?: number }).status;
    if (status === 403 || status === 404) {
      groupAccessLost = true;
      audience = null;
      preview = null;
    }
    if (status === 409) {
      audience = null;
      checkedApprovals = {};
      preview = null;
    }
  }
  async function saveGroup(enabled = false) {
    groupBusy = true;
    groupError = '';
    preview = null;
    try {
      const saved = await saveGroupBriefing(
        targetId,
        { ...groupPayload, enabled },
        groupSaved?.name,
      );
      groupSaved = saved;
      excludedUsers = saved.excluded_user_ids ?? excludedUsers;
      savedPayload = JSON.stringify(groupPayload);
      checkedApprovals = {};
      audience = await getBriefingAudience(targetId, saved.name);
      await refreshGroups();
      briefingsRefreshNonce.update((n) => n + 1);
    } catch (e) {
      handleGroupError(e);
    } finally {
      groupBusy = false;
    }
  }
  async function changeApproval(state: BriefingSourceApproval, revoke = false) {
    if (state.owner_id !== identity.user.username) return;
    groupBusy = true;
    groupError = '';
    preview = null;
    try {
      if (revoke) await revokeBriefingSource(targetId, groupName, state);
      else
        await approveBriefingSource(
          targetId,
          groupName,
          state,
          contributionDrafts[state.source_id],
        );
      await openGroup(groupSaved!);
      await refreshGroups();
    } catch (e) {
      handleGroupError(e);
    } finally {
      groupBusy = false;
    }
  }
  async function previewGroup() {
    groupBusy = true;
    groupError = '';
    preview = null;
    try {
      preview = await runGroupBriefing(targetId, groupName, 'preview', crypto.randomUUID());
    } catch (e) {
      handleGroupError(e);
    } finally {
      groupBusy = false;
    }
  }
  $effect(() => {
    if (
      !groupEditing ||
      groupDirty ||
      !preview ||
      !['queued', 'researching', 'generating'].includes(preview.state)
    )
      return;
    const runId = preview.id;
    const group = targetId;
    const name = groupName;
    let current = true;
    const timer = setTimeout(async () => {
      try {
        const result = await getGroupBriefingRun(group, name, runId);
        if (current) preview = result;
      } catch (e) {
        if (current) {
          preview = null;
          handleGroupError(e);
        }
      }
    }, 1000);
    return () => {
      current = false;
      clearTimeout(timer);
    };
  });
  function duplicateForGroup(b?: UserBriefingRow) {
    const old = b
      ? (JSON.parse(JSON.stringify(copy?.blocks ?? [])) as BriefingBlock[])
      : (JSON.parse(JSON.stringify(groupBlocks)) as BriefingBlock[]);
    const name = b?.name ?? groupName;
    const title = b?.title ?? groupTitle;
    const cron = b?.cron ?? groupCron;
    const mode = b ? 'blocks' : groupMode;
    const exclusions = b ? [] : [...excludedUsers];
    const prompt = generationPrompt;
    const research = researchPrompt;
    const researchOn = researchEnabled;
    startNew();
    excludedUsers = exclusions;
    groupName = `${name}-copy-${crypto.randomUUID().slice(0, 8)}`;
    groupTitle = title;
    groupCron = cron;
    groupMode = mode;
    if (!b) {
      generationPrompt = prompt;
      researchPrompt = research;
      researchEnabled = researchOn;
    }
    groupBlocks = old;
    for (const block of groupBlocks)
      for (const source of block.sources) sourceKeys[source.id] = `source-${crypto.randomUUID()}`;
    draftId = Math.max(
      draftId,
      ...groupBlocks.flatMap((block) => [block.id, ...block.sources.map((s) => s.id)]),
    );
    const first = targets.find((t) => t.scope === 'group' && t.capabilities.author_briefings);
    if (first) void selectTarget(first.group_id!);
    groupError =
      'This copy is a disabled draft. Review every source for the new audience; personal sources need fresh approval.';
  }
  let groupLoadStarted = false;
  $effect(() => {
    if (groupLoadStarted) return;
    groupLoadStarted = true;
    void refreshGroups()
      .then(async () => {
        const params = page.url.searchParams;
        const group = params.get('group');
        const name = params.get('briefing');
        if (group && name) {
          const definition = groupDefinitions.find((d) => d.group_id === group && d.name === name);
          if (definition) await openGroup(definition);
        } else if (params.has('new')) {
          startNew();
          if (group && targets.some((t) => t.group_id === group && t.capabilities.author_briefings))
            await selectTarget(group);
        }
      })
      .catch((e) => {
        groupError = (e as Error).message || 'Could not load group briefings.';
      });
  });

  function updateBlocks(_name: string, blocks: BriefingBlock[]) {
    for (const block of blocks)
      for (const source of block.sources) {
        if (!sourceKeys[source.id]) sourceKeys[source.id] = `source-${crypto.randomUUID()}`;
      }
    groupBlocks = blocks;
  }
  $effect(() => {
    onGroupActive?.(groupEditing);
  });
  let copied: typeof copy;
  $effect(() => {
    if (copy && copied !== copy) {
      copied = copy;
      untrack(() => duplicateForGroup(copy!.briefing));
    }
  });
  const groupTemplates = $derived([
    ...templates.filter((t) => t.block.sources.every((s) => groupSourceKinds.includes(s.kind))),
    {
      id: 'group-notebook',
      label: 'Group notebook',
      description: "This group's selected notebook.",
      block: {
        title: 'Group notebook',
        directive: '',
        render_mode: 'structured',
        options: {},
        sources: [{ kind: 'group_memory', config: {} }],
      },
    },
  ]);
</script>

<SettingsCard title="Briefings">
  <Button variant="primary" onclick={startNew} disabled={groupBusy}>New briefing</Button>
  {#if groupError}<div class="banner error" role="alert">{groupError}</div>{/if}
  {#each groupDefinitions as definition (`${definition.group_id}/${definition.name}`)}
    <div class="control-row">
      <span
        >{definition.title || definition.name} — {targets.find(
          (t) => t.group_id === definition.group_id,
        )?.label} · {channelLabels[definition.channel]} · {definition.cron} ({definition.timezone})
        · {definition.enabled ? 'Enabled' : 'Draft'}</span
      >
      {#if definition.capabilities.author_briefings || definition.source_approvals?.some((s) => s.owner_id === identity.user.username)}
        <Button size="sm" onclick={() => openGroup(definition)} disabled={groupBusy}
          >{definition.capabilities.author_briefings
            ? `Edit ${definition.name}`
            : `Review sources for ${definition.name}`}</Button
        >
      {/if}
    </div>
  {/each}
</SettingsCard>
{#if editorOpen}
  <SettingsCard title={groupSaved ? `Edit ${groupSaved.name}` : 'New briefing'}>
    {#if groupEditing && groupAuthor}
      <SettingsField label="Name"
        ><Input bind:value={groupName} disabled={!!groupSaved || groupBusy} /></SettingsField
      >
      <SettingsField label="Title"
        ><Input bind:value={groupTitle} disabled={groupBusy} /></SettingsField
      >
    {/if}
    <SettingsField
      label="Distribution target"
      hint={groupSaved
        ? 'Saved targets are fixed. Duplicate for group to choose another audience.'
        : undefined}
      labelled={false}
    >
      <Select
        value={targetId}
        options={targets
          .filter((t) => t.capabilities.author_briefings || t.group_id === targetId)
          .map((t) => ({
            value: t.group_id ?? '',
            label: t.scope === 'user' ? 'Just me' : `${t.label} (${t.member_count} members)`,
          }))}
        onValueChange={selectTarget}
        disabled={!!groupSaved || groupBusy}
        ariaLabel="Distribution target"
        fullWidth
      />
    </SettingsField>
    {#if groupEditing}
      <div class="banner info" role="note">
        Generated once per run. The same briefing is sent to members of {groupTarget?.label} via {channelLabels[
          groupChannel
        ]}. Each member uses their own configured destination. Every published edition remains
        available in app.
        {#if groupTarget?.system_key === 'all_users'}Everyone currently using this installation and
          anyone who joins later can read retained editions.{:else}Current and future members can
          read retained editions.{/if}
      </div>
      {#if groupAuthor}
        <SettingsField label="Excluded users" labelled={false}>
          <MultiSelect
            value={excludedUsers}
            options={memberOptions}
            onValueChange={(next) => (excludedUsers = next)}
            disabled={groupBusy}
            placeholder="Search members to exclude…"
            emptyLabel="No members excluded."
            ariaLabel="Excluded users"
          />
          <p class="caption">
            Excluded members receive no deliveries or in-app notifications for this briefing. They
            can still read its editions in group history.
          </p>
        </SettingsField>
        <div class="add-grid">
          <SettingsField label="Delivery channel" labelled={false}
            ><Select
              value={groupChannel}
              options={channels.map((c) => ({ value: c, label: channelLabels[c] }))}
              onValueChange={(v) => {
                groupChannel = v;
                checkedApprovals = {};
                audienceAcknowledged = false;
              }}
              disabled={groupBusy}
              ariaLabel="Delivery channel"
              fullWidth
            /></SettingsField
          >
          <SettingsField label="Cron"
            ><Input bind:value={groupCron} disabled={groupBusy} /></SettingsField
          >
          <SettingsField label="Timezone" hint="IANA timezone, for example Europe/Berlin."
            ><Input bind:value={groupTimezone} disabled={groupBusy} /></SettingsField
          >
          <SettingsField label="Content" labelled={false}
            ><Select
              value={groupMode}
              options={[
                { value: 'prompt', label: 'Generate from prompt' },
                { value: 'blocks', label: 'Build from blocks' },
              ]}
              onValueChange={(v) => {
                groupMode = v as 'prompt' | 'blocks';
              }}
              disabled={groupBusy}
              ariaLabel="Content"
              fullWidth
            /></SettingsField
          >
        </div>
        {#if groupMode === 'prompt'}
          <SettingsField
            label="Generation prompt"
            wide
            hint="Used for the final edition with selected inputs. This prompt is not sent to public research tools."
            ><textarea rows="4" bind:value={generationPrompt} disabled={groupBusy}
            ></textarea></SettingsField
          >
          <SettingsField label="Public research" checkbox
            ><input
              type="checkbox"
              bind:checked={researchEnabled}
              disabled={groupBusy}
            /></SettingsField
          >
          {#if researchEnabled}<SettingsField
              label="Public research prompt"
              wide
              hint="Public information only. Research uses its own browser profile, without your login sessions."
              ><textarea rows="3" bind:value={researchPrompt} disabled={groupBusy}
              ></textarea></SettingsField
            >{/if}
        {/if}
        <SettingsField
          label="My prompt or directives contain personal information"
          checkbox
          hint="Free text is shared with the same audience as selected sources."
          ><input
            type="checkbox"
            bind:checked={personalInformation}
            disabled={groupBusy}
          /></SettingsField
        >
        {#if personalInformation}<SettingsField
            label="I approve recurring disclosure of this text to current and future members, including retained editions and copies that cannot be recalled"
            checkbox
            ><input
              type="checkbox"
              bind:checked={audienceAcknowledged}
              disabled={groupBusy}
            /></SettingsField
          >{/if}
        {#if groupSaved}<Button size="sm" onclick={() => duplicateForGroup()} disabled={groupBusy}
            >Duplicate for group</Button
          >{/if}
      {:else}<p>
          You can review your own contributions. Only current group owners can edit this briefing.
        </p>{/if}
    {:else}<p>Just me: use the personal schedule and content blocks below.</p>{/if}
  </SettingsCard>
{/if}

{#if groupEditing && groupAuthor}
  <SettingsCard title={groupMode === 'prompt' ? 'Selected inputs' : 'Content blocks'}>
    {#each groupBlocks.flatMap((block) => block.sources) as source (source.id)}
      <p class="caption">
        {sourceScopes[source.kind] ?? 'Unsupported source'}: {source.kind}{source.config.owner_id
          ? ` · ${source.config.owner_id}`
          : ''}{source.config.path || source.config.calendar_url
          ? ` · ${source.config.path ?? source.config.calendar_url}`
          : ''}
      </p>
    {/each}
    <fieldset disabled={groupBusy} class="draft-fields">
      {#key `${targetId}/${groupSaved?.id ?? 'new'}`}
        <BlockBuilder
          briefingName={groupName}
          blocks={groupBlocks}
          templates={groupTemplates}
          sourceKinds={groupSourceKinds}
          {browseOptions}
          rssOptions={[]}
          {sharedBlockOptions}
          feedsAvailable={false}
          localDraft
          userId={identity.user.username}
          fileOptions={resourceOptions.files}
          calendarOptions={resourceOptions.calendars}
          onChange={updateBlocks}
        />
      {/key}
    </fieldset>
  </SettingsCard>
{/if}
{#if groupEditing}
  <SettingsCard title="Audience and source review">
    <p>
      {groupTarget?.member_count ?? 0} current members. Current and future members can read retained editions.
      Sent and downloaded copies cannot be recalled.
    </p>
    {#if groupInvalidSources.length}
      <div class="banner warning">
        These personal sources cannot run for a group. Remove them or replace them with an
        explicitly approved personal file or calendar contribution.
      </div>
      {#each groupInvalidSources as source (source.id)}<p>
          {source.kind}: {String(source.config.path ?? source.config.owner_id ?? '')}
        </p>{/each}
    {/if}
    {#if groupAuthor && groupDirty}<p>
        Save a disabled draft to refresh recipient counts and review exact source approvals before
        enabling or previewing.
      </p>{/if}
    {#if audience && !groupDirty}
      {#if groupAuthor}<p>
          {audience.member_count} members · {audience.deliverable} deliverable · {audience.muted} muted
          · {audience.missing_destination} missing destinations · {audience.excluded} excluded
        </p>{/if}
      {#each audience.sources as approval (approval.source_id)}
        <div class="banner warning" role="note">
          <p>{approval.warning}</p>
          <p>
            {approval.approved
              ? 'Sharing approved. This disclosure still applies on every run.'
              : 'Sharing approval required.'}
          </p>
          {#if approval.owner_id === identity.user.username && !groupAccessLost}
            {#if !approval.approved}
              <SourceConfigFields
                kind={approval.resource.kind}
                config={contributionDrafts[approval.source_id] ?? approval.resource.config}
                onChange={(patch) => {
                  contributionDrafts[approval.source_id] = {
                    ...(contributionDrafts[approval.source_id] ?? approval.resource.config),
                    ...patch,
                  };
                  checkedApprovals[approval.source_id] = false;
                }}
                userId={identity.user.username}
                fileOptions={resourceOptions.files}
                calendarOptions={resourceOptions.calendars}
                ownerFixed
              />
              {#if contributionDrafts[approval.source_id]}
                <p>
                  Selected resource: {String(
                    contributionDrafts[approval.source_id].path ??
                      contributionDrafts[approval.source_id].calendar_url ??
                      'Not selected',
                  )}. {approval.resource.kind === 'personal_file'
                    ? 'The full file may be summarized or quoted on each run.'
                    : 'Only the fields and date window selected above will be disclosed on each run.'}
                </p>
              {/if}
              <SettingsField
                label={`I approve recurring sharing of ${approval.source_id} with this audience and channel`}
                checkbox
                ><input
                  type="checkbox"
                  bind:checked={checkedApprovals[approval.source_id]}
                  disabled={groupBusy}
                /></SettingsField
              >
              <Button
                size="sm"
                disabled={groupBusy || !checkedApprovals[approval.source_id]}
                onclick={() => changeApproval(approval)}>Approve source</Button
              >
            {:else}<Button
                size="sm"
                disabled={groupBusy}
                onclick={() => changeApproval(approval, true)}>Revoke source approval</Button
              >{/if}
          {:else}<p>Only {approval.owner_id} can approve or revoke this source.</p>{/if}
        </div>
      {/each}
    {/if}
    {#if resourceOptions.calendar_unavailable}<p>
        Calendar discovery is unavailable. Retry opening this briefing to load your calendars.
      </p>{/if}
    {#if groupAuthor}
      <div class="control-row">
        <Button
          variant="primary"
          disabled={groupBusy ||
            groupInvalidSources.length > 0 ||
            !groupName.trim() ||
            (groupMode === 'prompt' &&
              (!generationPrompt.trim() || (researchEnabled && !researchPrompt.trim())))}
          onclick={() => saveGroup(false)}>Save draft</Button
        >
        <Button disabled={groupBusy || !groupReady} onclick={() => saveGroup(true)}
          >Enable schedule</Button
        >
        <Button
          disabled={groupBusy ||
            !groupReady ||
            groupTarget?.capabilities.generate_briefings === false}
          onclick={previewGroup}>Preview</Button
        >
        {#if groupSaved}<Button disabled={groupBusy} onclick={() => openGroup(groupSaved!)}
            >Reload saved briefing</Button
          >{/if}
      </div>
    {/if}
    <p>
      Preview generates a private draft and sends nothing. Publishing requires a fresh generation.
    </p>
    {#if preview && !groupDirty}
      <p role="status">
        Preview: {preview.state}{preview.error_code ? ` (${preview.error_code})` : ''}
      </p>
      {#if preview.state === 'preview_ready'}<BriefingEdition
          showMetadata={false}
          edition={{
            id: preview.id,
            briefing_name: groupName,
            subject: preview.subject ?? groupTitle,
            body_md: preview.body_md,
            generated_at: preview.created_at ?? '',
            task_id: null,
            delivered_to: [],
          }}
        />{/if}
    {/if}
  </SettingsCard>
{/if}

<style>
  .add-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(160px, 100%), 1fr));
    gap: var(--space-2);
  }
  .draft-fields {
    border: 0;
    padding: 0;
    margin: 0;
    min-width: 0;
  }
</style>
