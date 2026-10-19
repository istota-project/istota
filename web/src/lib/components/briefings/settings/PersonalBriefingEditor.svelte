<script lang="ts">
  import { untrack } from 'svelte';
  import {
    upsertBriefing,
    type BriefingBlock,
    type BriefingBlockTemplate,
    type BriefingRoomOption,
    type EmailAccount,
    type UserBriefingRow,
  } from '$lib/api';
  import { Input, NoticeBanner, Select, type SelectOption, Card, Field } from '$lib/components/ui';
  import { notifyError } from '$lib/stores/notices';
  import { useSettingsSave } from '$lib/stores/settingsSave.svelte';
  import { briefingsRefreshNonce } from '$lib/stores/briefings';
  import BlockBuilder from '../BlockBuilder.svelte';
  import { channelLabel, derivedTitle } from './briefingSettings';

  let {
    name,
    schedule,
    blocks,
    templates,
    sourceKinds,
    browseOptions,
    rssOptions,
    sharedBlockOptions,
    feedsAvailable,
    emailAccounts = [],
    outputs,
    rooms,
    editBlockId = null,
    onSaved,
    onBlocksChange,
  }: {
    name: string;
    schedule: UserBriefingRow | undefined;
    blocks: BriefingBlock[];
    templates: BriefingBlockTemplate[];
    sourceKinds: string[];
    browseOptions: SelectOption[];
    rssOptions: SelectOption[];
    sharedBlockOptions: SelectOption[];
    feedsAvailable: boolean;
    emailAccounts?: EmailAccount[];
    outputs: string[];
    rooms: BriefingRoomOption[];
    editBlockId?: number | null;
    onSaved: () => Promise<void>;
    onBlocksChange: (name: string, blocks: BriefingBlock[]) => void;
  } = $props();

  type Draft = {
    title: string;
    cron: string;
    output: string;
    conversation_token: string;
    enabled: boolean;
  };
  const fromSchedule = (row: UserBriefingRow | undefined): Draft => ({
    title: row?.title ?? '',
    cron: row?.cron ?? '0 7 * * *',
    output: row?.output ?? outputs[0] ?? 'talk',
    conversation_token: row?.conversation_token ?? '',
    enabled: row?.enabled ?? false,
  });
  let draft = $state<Draft>(fromSchedule(undefined));
  let saved = $state('');
  let saving = $state(false);
  // Reseed when the server copy changes, never under an edit in progress. Only
  // `schedule` is tracked: the draft is both read and written here.
  $effect(() => {
    const next = fromSchedule(schedule);
    untrack(() => {
      if (!saved || JSON.stringify(draft) === saved) draft = next;
      saved = JSON.stringify(next);
    });
  });

  const held = $derived(!!schedule?.held);
  const dirty = $derived(!!saved && JSON.stringify(draft) !== saved);
  const outputOptions = $derived(
    [...new Set([...outputs, draft.output])].map((o) => ({ value: o, label: channelLabel(o) })),
  );

  async function save() {
    if (!draft.cron.trim()) {
      notifyError('Choose a schedule.');
      return;
    }
    if (draft.output === 'talk' && !draft.conversation_token) {
      notifyError('Choose a Talk room.');
      return;
    }
    saving = true;
    try {
      await upsertBriefing({
        name,
        title: draft.title.trim(),
        cron: draft.cron.trim(),
        output: draft.output,
        conversation_token: draft.output === 'talk' ? draft.conversation_token : undefined,
        enabled: draft.enabled,
      });
      saved = JSON.stringify(draft);
      briefingsRefreshNonce.update((n) => n + 1);
      await onSaved();
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Could not save the schedule.');
    } finally {
      saving = false;
    }
  }

  useSettingsSave(() => ({ dirty, saving, save }));
</script>

<Card title="Schedule and delivery">
  {#if held}
    <NoticeBanner title="Paused by an administrator" variant="warn" collapsed={false}>
      This briefing will not run until an administrator resumes it.
    </NoticeBanner>
  {/if}
  <fieldset class="fields">
    <Field
      label="Title"
      hint="Shown on every edition, followed by its date. Leave blank to use the name."
    >
      <Input bind:value={draft.title} placeholder={derivedTitle(name)} />
    </Field>
    <div class="field-row">
      <Field
        label="Schedule"
        hint="A cron expression in your timezone, for example 0 7 * * 1-5 for weekdays at 07:00."
      >
        <Input bind:value={draft.cron} monospace />
      </Field>
      <Field label="Delivery">
        <Select
          value={draft.output}
          options={outputOptions}
          onValueChange={(v) => (draft.output = v)}
          ariaLabel="Delivery"
          fullWidth
        />
      </Field>
      {#if draft.output === 'talk'}
        <Field label="Talk room">
          <Select
            value={draft.conversation_token}
            options={[
              ...rooms.map((r) => ({ value: r.token, label: r.name })),
              ...(draft.conversation_token &&
              !rooms.some((r) => r.token === draft.conversation_token)
                ? [{ value: draft.conversation_token, label: draft.conversation_token }]
                : []),
            ]}
            onValueChange={(v) => (draft.conversation_token = v)}
            placeholder="Choose a room"
            ariaLabel="Talk room"
            fullWidth
          />
        </Field>
      {/if}
    </div>
    <Field
      label="Enabled"
      checkbox
      hint="When off, the briefing keeps its content but does not run."
    >
      <input type="checkbox" bind:checked={draft.enabled} disabled={held} />
    </Field>
  </fieldset>
</Card>

<Card
  title="Content"
  description="Ordered blocks, each gathered from its sources and summarized or shown as is. Changes save as you make them."
>
  {#key name}
    <BlockBuilder
      briefingName={name}
      {editBlockId}
      {blocks}
      {templates}
      {sourceKinds}
      {browseOptions}
      {rssOptions}
      {sharedBlockOptions}
      {feedsAvailable}
      {emailAccounts}
      onChange={onBlocksChange}
    />
  {/key}
</Card>

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
</style>
