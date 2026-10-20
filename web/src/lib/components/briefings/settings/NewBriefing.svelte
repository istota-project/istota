<script lang="ts">
  import { goto } from '$app/navigation';
  import {
    saveGroupBriefing,
    upsertBriefing,
    type BriefingRoomOption,
    type BriefingTarget,
  } from '$lib/api';
  import {
    Button,
    Input,
    OptionCards,
    Select,
    type OptionCard,
    Card,
    Field,
    TextArea,
  } from '$lib/components/ui';
  import { briefingsRefreshNonce } from '$lib/stores/briefings';
  import { notifyError } from '$lib/stores/notices';
  import { briefingSettingsUrl, channelLabel, derivedTitle, slugify } from './briefingSettings';

  let {
    targets,
    channels,
    timezone: deploymentTimezone,
    outputs,
    rooms,
    initialGroup = '',
    taken = [],
  }: {
    /** Every target from `/briefings/targets`, the personal one included. */
    targets: BriefingTarget[];
    channels: string[];
    timezone: string;
    outputs: string[];
    rooms: BriefingRoomOption[];
    initialGroup?: string;
    /** Personal briefing names already in use. */
    taken?: string[];
  } = $props();

  const authorable = $derived(
    targets.filter((t) => t.scope === 'group' && t.capabilities.author_briefings),
  );
  let audience = $state('user');
  $effect(() => {
    if (initialGroup && authorable.some((t) => t.group_id === initialGroup))
      audience = initialGroup;
  });
  const group = $derived(authorable.find((t) => t.group_id === audience) ?? null);

  let title = $state('');
  let cron = $state('0 7 * * *');
  let timezone = $state('');
  let output = $state('');
  let room = $state('');
  let channel = $state('in_app');
  let prompt = $state('');
  let saving = $state(false);

  const name = $derived(slugify(title));
  const zone = $derived(timezone || deploymentTimezone);
  const personalOutput = $derived(output || outputs[0] || 'talk');

  const audienceOptions = $derived<OptionCard[]>([
    {
      value: 'user',
      title: 'Just me',
      description: 'Built from your own sources and delivered only to you.',
    },
    ...authorable.map((t) => ({
      value: t.group_id!,
      title: t.label,
      badge: `${t.member_count ?? 0} members`,
      description:
        t.system_key === 'all_users'
          ? 'Generated once and sent to everyone on this installation, including people who join later.'
          : 'Generated once and sent to every member. Future members can read past editions.',
    })),
  ]);

  const problem = $derived.by(() => {
    if (!name) return 'Give the briefing a title.';
    if (!group && taken.includes(name)) return 'You already have a briefing with this name.';
    if (!cron.trim()) return 'Choose a schedule.';
    if (!group && personalOutput === 'talk' && !room) return 'Choose a Talk room.';
    if (group && !prompt.trim()) return 'Describe what the briefing should cover.';
    return '';
  });

  async function create() {
    if (problem || saving) return;
    saving = true;
    try {
      if (group) {
        const saved = await saveGroupBriefing(group.group_id!, {
          name,
          title: title.trim(),
          cron: cron.trim(),
          timezone: zone,
          channel,
          excluded_user_ids: [],
          revision: 0,
          enabled: false,
          generation_mode: 'prompt',
          generation: {
            version: 1,
            contains_personal_information: false,
            audience_acknowledged: false,
            prompt: prompt.trim(),
            research: { enabled: false, prompt: '' },
            inputs: [],
          },
        });
        briefingsRefreshNonce.update((n) => n + 1);
        await goto(
          briefingSettingsUrl({ kind: 'group', group: group.group_id!, name: saved.name }),
        );
      } else {
        await upsertBriefing({
          name,
          title: title.trim(),
          cron: cron.trim(),
          output: personalOutput,
          conversation_token: personalOutput === 'talk' ? room : undefined,
          enabled: false,
        });
        briefingsRefreshNonce.update((n) => n + 1);
        await goto(briefingSettingsUrl({ kind: 'personal', name }));
      }
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Could not create the briefing.');
    } finally {
      saving = false;
    }
  }
</script>

<Card title="Who is it for?">
  <OptionCards options={audienceOptions} bind:value={audience} label="Who the briefing is for" />
</Card>

<Card
  title="Basics"
  description="It is created switched off, so nothing runs until you have added its content and turned it on."
>
  <Field
    label="Title"
    hint="Shown on every edition, followed by its date. The briefing's name is derived from it."
  >
    <Input bind:value={title} placeholder="Morning news" />
  </Field>
  <div class="field-row">
    <Field
      label="Schedule"
      hint="A cron expression, for example 0 7 * * 1-5 for weekdays at 07:00."
    >
      <Input bind:value={cron} monospace />
    </Field>
    {#if group}
      <Field label="Timezone" hint="One schedule for everyone in the group.">
        <Input bind:value={timezone} placeholder={deploymentTimezone} />
      </Field>
    {/if}
  </div>
  {#if group}
    <Field
      label="Delivery"
      hint="Each member receives it at their own address for this channel. Every edition is also readable in Briefings."
    >
      <Select
        value={channel}
        options={channels.map((c) => ({ value: c, label: channelLabel(c) }))}
        onValueChange={(v) => (channel = v)}
        ariaLabel="Delivery"
        fullWidth
      />
    </Field>
    <Field
      label="What should it cover?"
      hint="The instructions for each edition. You can switch to building it from blocks afterwards."
      wide
    >
      <TextArea
        rows={4}
        bind:value={prompt}
        placeholder="A short morning briefing on what the team shipped and what is blocked."
      />
    </Field>
  {:else}
    <div class="field-row">
      <Field label="Delivery">
        <Select
          value={personalOutput}
          options={outputs.map((o) => ({ value: o, label: channelLabel(o) }))}
          onValueChange={(v) => (output = v)}
          ariaLabel="Delivery"
          fullWidth
        />
      </Field>
      {#if personalOutput === 'talk'}
        <Field label="Talk room">
          <Select
            value={room}
            options={rooms.map((r) => ({ value: r.token, label: r.name }))}
            onValueChange={(v) => (room = v)}
            placeholder="Choose a room"
            ariaLabel="Talk room"
            fullWidth
          />
        </Field>
      {/if}
    </div>
  {/if}
  <div class="actions">
    <span class="caption"
      >{problem || `Saved as ${name}${group ? '' : ` · ${derivedTitle(name)}`}`}</span
    >
    <Button
      variant="primary"
      onclick={create}
      disabled={!!problem}
      loading={saving}
      loadingLabel="Creating…">Create briefing</Button
    >
  </div>
</Card>

<style>
  .field-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(14rem, 100%), 1fr));
    gap: var(--space-3);
    align-items: start;
  }
  .actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    margin-top: var(--space-2);
  }
</style>
