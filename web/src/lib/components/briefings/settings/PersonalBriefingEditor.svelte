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
  import {
    Input,
    Alert,
    Select,
    type SelectOption,
    Card,
    Field,
    FormGrid,
    FormStack,
    Checkbox,
  } from '$lib/components/ui';
  import { createDraft } from '$lib/ui/draft.svelte';
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
    feedsFailed = false,
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
    feedsFailed?: boolean;
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
  const form = createDraft<Draft>(fromSchedule(undefined));
  let saving = $state(false);
  // Reseed when the server copy changes, never under an edit in progress: an
  // edit survives as the value over the new saved copy. Only `schedule` is
  // tracked: the draft is both read and written here.
  $effect(() => {
    const next = fromSchedule(schedule);
    untrack(() => {
      const edits = form.dirty ? form.value : null;
      form.reset(next);
      if (edits) form.value = edits;
    });
  });

  const held = $derived(!!schedule?.held);
  const outputOptions = $derived(
    [...new Set([...outputs, form.value.output])].map((o) => ({
      value: o,
      label: channelLabel(o),
    })),
  );

  async function save() {
    if (!form.value.cron.trim()) {
      notifyError('Choose a schedule.');
      return;
    }
    if (form.value.output === 'talk' && !form.value.conversation_token) {
      notifyError('Choose a Talk room.');
      return;
    }
    saving = true;
    try {
      await upsertBriefing({
        name,
        title: form.value.title.trim(),
        cron: form.value.cron.trim(),
        output: form.value.output,
        conversation_token:
          form.value.output === 'talk' ? form.value.conversation_token : undefined,
        enabled: form.value.enabled,
      });
      form.reset();
      briefingsRefreshNonce.update((n) => n + 1);
      await onSaved();
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Could not save the schedule.');
    } finally {
      saving = false;
    }
  }

  useSettingsSave(() => ({ dirty: form.dirty, saving, save }));
</script>

<Card title="Schedule and delivery">
  {#if held}
    <Alert title="Paused by an administrator" variant="warn" collapsed={false}>
      This briefing will not run until an administrator resumes it.
    </Alert>
  {/if}
  <FormStack>
    <Field
      label="Title"
      hint="Shown on every edition, followed by its date. Leave blank to use the name."
    >
      <Input bind:value={form.value.title} placeholder={derivedTitle(name)} />
    </Field>
    <FormGrid min="12rem">
      <Field
        label="Schedule"
        hint="A cron expression in your timezone, for example 0 7 * * 1-5 for weekdays at 07:00."
      >
        <Input bind:value={form.value.cron} monospace />
      </Field>
      <Field label="Delivery">
        <Select
          value={form.value.output}
          options={outputOptions}
          onValueChange={(v) => (form.value.output = v)}
          ariaLabel="Delivery"
          fullWidth
        />
      </Field>
      {#if form.value.output === 'talk'}
        <Field label="Talk room">
          <Select
            value={form.value.conversation_token}
            options={[
              ...rooms.map((r) => ({ value: r.token, label: r.name })),
              ...(form.value.conversation_token &&
              !rooms.some((r) => r.token === form.value.conversation_token)
                ? [{ value: form.value.conversation_token, label: form.value.conversation_token }]
                : []),
            ]}
            onValueChange={(v) => (form.value.conversation_token = v)}
            placeholder="Choose a room"
            ariaLabel="Talk room"
            fullWidth
          />
        </Field>
      {/if}
    </FormGrid>
    <Field
      label="Enabled"
      checkbox
      hint="When off, the briefing keeps its content but does not run."
    >
      <Checkbox bind:checked={form.value.enabled} disabled={held} />
    </Field>
  </FormStack>
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
      {feedsFailed}
      {emailAccounts}
      onChange={onBlocksChange}
    />
  {/key}
</Card>
