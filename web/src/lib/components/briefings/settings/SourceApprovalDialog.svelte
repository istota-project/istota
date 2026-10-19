<script lang="ts">
  import { untrack } from 'svelte';
  import type { BriefingSourceApproval } from '$lib/api';
  import { Button, Modal, type SelectOption, Field, Checkbox } from '$lib/components/ui';
  import SourceConfigFields from '../SourceConfigFields.svelte';
  import { calendarDisclosure, channelLabel } from './briefingSettings';

  // The resource owner's own approval of one contributed source. The facts are
  // laid out one per line so each part of the disclosure can be read on its own;
  // the confirmation is never preselected.
  let {
    approval,
    userId,
    groupLabel,
    memberCount,
    allUsers,
    channel,
    files,
    calendars,
    busy = false,
    onApprove,
    onRevoke,
    onClose,
  }: {
    approval: BriefingSourceApproval;
    userId: string;
    groupLabel: string;
    memberCount: number;
    allUsers: boolean;
    channel: string;
    files: string[];
    calendars: SelectOption[];
    busy?: boolean;
    onApprove: (resource: Record<string, unknown>) => void;
    onRevoke: () => void;
    onClose: () => void;
  } = $props();

  // A draft seeded once: the dialog is mounted per approval it reviews.
  let config = $state<Record<string, unknown>>(untrack(() => ({ ...approval.resource.config })));
  let agreed = $state(false);
  const calendar = $derived(approval.resource.kind === 'personal_calendar');
  const chosen = $derived(calendar ? !!config.calendar_url : !!String(config.path ?? '').trim());
  const audience = $derived(
    allUsers
      ? 'Everyone on this installation, including people who join later'
      : `${groupLabel} (${memberCount} ${memberCount === 1 ? 'member' : 'members'}), including future members`,
  );
</script>

<Modal
  open={true}
  title={approval.approved
    ? `Your shared ${calendar ? 'calendar' : 'file'}`
    : `Share your ${calendar ? 'calendar' : 'file'}`}
  description="A group briefing uses this on every run."
  onOpenChange={(open) => {
    if (!open) onClose();
  }}
  width="36rem"
>
  <div class="approval">
    {#if !approval.approved}
      <SourceConfigFields
        kind={approval.resource.kind}
        {config}
        onChange={(patch) => {
          config = { ...config, ...patch };
          agreed = false;
        }}
        {userId}
        fileOptions={files}
        calendarOptions={calendars}
        ownerFixed
      />
    {/if}
    <dl class="kv facts">
      <dt>Shared with</dt>
      <dd>{audience}</dd>
      <dt>Delivered by</dt>
      <dd>{channelLabel(channel)}, and readable in Briefings</dd>
      <dt>What is shared</dt>
      <dd>
        {calendar
          ? `Events: ${calendarDisclosure(config)}`
          : 'The whole file, which may be summarized or quoted'}
      </dd>
      <dt>How often</dt>
      <dd>Every time the briefing runs</dd>
      <dt>If you stop</dt>
      <dd>
        Future runs stop using it. Past editions stay in the group's history, and copies already
        sent cannot be recalled.
      </dd>
    </dl>
    {#if !approval.approved}
      <Field label="I agree to share this on every run" checkbox>
        <Checkbox bind:checked={agreed} disabled={!chosen} />
      </Field>
    {/if}
  </div>
  {#snippet footer()}
    <Button variant="secondary" onclick={onClose}>Cancel</Button>
    {#if approval.approved}
      <Button variant="danger" onclick={onRevoke} loading={busy} loadingLabel="Stopping…"
        >Stop sharing</Button
      >
    {:else}
      <Button
        variant="primary"
        onclick={() => onApprove(config)}
        disabled={!agreed || !chosen}
        loading={busy}
        loadingLabel="Approving…">Approve</Button
      >
    {/if}
  {/snippet}
</Modal>

<style>
  .approval {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
  }
  .facts {
    --kv-gap: var(--space-4);
    row-gap: var(--space-2);
    margin: 0;
  }
  .facts dt {
    color: var(--text-muted);
  }
  .facts dd {
    margin: 0;
  }
</style>
