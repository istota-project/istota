<script lang="ts">
  import { goto } from '$app/navigation';
  import {
    getGroup,
    saveGroupBriefing,
    type BriefingTarget,
    type GroupBriefingGeneration,
  } from '$lib/api';
  import {
    FormModal,
    Alert,
    OptionCards,
    type OptionCard,
    Field,
    TextArea,
  } from '$lib/components/ui';
  import { briefingsRefreshNonce } from '$lib/stores/briefings';
  import { briefingSettingsUrl, GROUP_SOURCE_KINDS } from './briefingSettings';

  type SourceDraft = {
    kind: string;
    config: Record<string, unknown>;
    enabled?: boolean;
  };
  export type DuplicateSource = {
    name: string;
    title: string;
    cron: string;
    timezone: string;
    channel: string;
    excluded_user_ids: string[];
    mode: 'prompt' | 'blocks';
    prompt: string;
    research: { enabled: boolean; prompt: string };
    containsPersonalInformation: boolean;
    blocks: {
      title: string;
      directive: string | null;
      render_mode: string;
      options: Record<string, unknown>;
      sources: SourceDraft[];
    }[];
  };

  // A copy is always a new disabled draft in its new group: every source gets a
  // fresh identity, so no approval carries over, and the audience acknowledgement
  // is cleared. Sources a group cannot run are listed, never dropped silently.
  let {
    from,
    targets,
    excludeGroup = '',
    onClose,
  }: {
    from: DuplicateSource;
    targets: BriefingTarget[];
    excludeGroup?: string;
    onClose: () => void;
  } = $props();

  const GROUP_KINDS = GROUP_SOURCE_KINDS;
  const choices = $derived(
    targets.filter(
      (t) => t.scope === 'group' && t.capabilities.author_briefings && t.group_id !== excludeGroup,
    ),
  );
  let target = $state('');
  $effect(() => {
    if (!target && choices.length) target = choices[0].group_id!;
  });
  let prompt = $state('');

  const options = $derived<OptionCard[]>(
    choices.map((t) => ({
      value: t.group_id!,
      title: t.label,
      badge: `${t.member_count ?? 0} members`,
    })),
  );
  const dropped = $derived(
    from.blocks.flatMap((b) =>
      b.sources
        .filter((s) => !GROUP_KINDS.includes(s.kind) || s.enabled === false)
        .map((s) => `${b.title}: ${s.kind}${s.enabled === false ? ' (switched off)' : ''}`),
    ),
  );
  const keptBlocks = $derived(
    from.blocks
      .map((b) => ({
        ...b,
        sources: b.sources.filter((s) => GROUP_KINDS.includes(s.kind) && s.enabled !== false),
      }))
      .filter((b) => b.sources.length),
  );
  const asPrompt = $derived(from.mode === 'prompt' || keptBlocks.length === 0);
  const promptText = $derived(prompt || (from.mode === 'prompt' ? from.prompt : ''));

  async function create() {
    if (asPrompt && !promptText.trim()) throw new Error('Describe what the copy should cover.');
    const members = new Set((await getGroup(target)).members.map((m) => m.user_id));
    const rekey = (s: SourceDraft) => ({
      id: `source-${crypto.randomUUID()}`,
      kind: s.kind,
      config: s.config,
    });
    const generation: GroupBriefingGeneration = asPrompt
      ? {
          version: 1,
          contains_personal_information: from.containsPersonalInformation,
          audience_acknowledged: false,
          prompt: promptText.trim(),
          research: from.research,
          inputs: keptBlocks.flatMap((b) => b.sources.map(rekey)),
        }
      : {
          version: 1,
          contains_personal_information: from.containsPersonalInformation,
          audience_acknowledged: false,
          blocks: keptBlocks.map((b) => ({ ...b, sources: b.sources.map(rekey) })),
        };
    const saved = await saveGroupBriefing(target, {
      name: `${from.name}-copy-${crypto.randomUUID().slice(0, 8)}`,
      title: from.title,
      cron: from.cron,
      timezone: from.timezone,
      channel: from.channel,
      excluded_user_ids: from.excluded_user_ids.filter((id) => members.has(id)),
      revision: 0,
      enabled: false,
      generation_mode: asPrompt ? 'prompt' : 'blocks',
      generation,
    });
    briefingsRefreshNonce.update((n) => n + 1);
    onClose();
    await goto(briefingSettingsUrl({ kind: 'group', group: target, name: saved.name }));
  }
</script>

<FormModal
  open={true}
  title="Duplicate to a group"
  description="The copy starts switched off. Shared personal sources need their owners' approval again."
  width="34rem"
  submitLabel="Create copy"
  busyLabel="Creating…"
  canSubmit={!!target}
  onSubmit={create}
  onCancel={onClose}
>
  <div class="duplicate vstack">
    {#if choices.length}
      <OptionCards {options} bind:value={target} label="Group to copy into" />
    {:else}
      <p class="caption">You are not an owner of any group you could copy this into.</p>
    {/if}
    {#if dropped.length}
      <Alert title="Left out of the copy" collapsed={false}>
        These sources cannot run in a group briefing:
        <ul>
          {#each dropped as item}<li>{item}</li>{/each}
        </ul>
      </Alert>
    {/if}
    {#if asPrompt && from.mode !== 'prompt'}
      <Field
        label="What should it cover?"
        hint="Nothing in the original can be used by a group, so the copy starts from instructions."
        wide
      >
        <TextArea rows={3} bind:value={prompt} />
      </Field>
    {/if}
  </div>
</FormModal>

<style>
  .duplicate p,
  .duplicate ul {
    margin: 0;
  }
  .duplicate ul {
    padding-left: var(--space-4);
  }
</style>
