<script lang="ts">
  import { Users } from '@lucide/svelte';
  import {
    getGroupBriefing,
    getGroupBriefingRun,
    getGroupBriefingDelivery,
    type GroupBriefingDefinition,
    type GroupBriefingRun,
    type BriefingDelivery,
  } from '$lib/api';
  import { PageLoading } from '$lib/components/ui';
  import { formatDateTime } from '$lib/dateFormat';
  import type { GroupEditionSelection } from '$lib/stores/briefings';
  import BriefingEdition from './BriefingEdition.svelte';
  import { channelLabel } from './settings/briefingSettings';

  let { selection, groupLabel }: { selection: GroupEditionSelection; groupLabel: string } =
    $props();

  let definition = $state<GroupBriefingDefinition | null>(null);
  let run = $state<GroupBriefingRun | null>(null);
  let delivery = $state<BriefingDelivery | null>(null);
  let loading = $state(true);
  let error = $state('');

  const summary = $derived(
    delivery
      ? Object.entries(delivery.counts)
          .map(([state, count]) => `${count} ${state}`)
          .join(', ')
      : '',
  );

  async function load(target: GroupEditionSelection) {
    loading = true;
    error = '';
    try {
      const [live, item] = await Promise.all([
        getGroupBriefing(target.group, target.briefing),
        getGroupBriefingRun(target.group, target.briefing, target.run),
      ]);
      const status =
        item.state === 'published'
          ? await getGroupBriefingDelivery(target.group, target.briefing, target.run)
          : null;
      if (target !== selection) return;
      definition = live;
      run = item;
      delivery = status;
    } catch (e) {
      if (target !== selection) return;
      const status = (e as { status?: number })?.status;
      error =
        status === 403 || status === 404
          ? 'This edition is no longer available to you.'
          : 'Could not load this edition.';
    } finally {
      if (target === selection) loading = false;
    }
  }

  $effect(() => {
    definition = null;
    run = null;
    delivery = null;
    void load(selection);
  });
</script>

{#if loading && !run}
  <PageLoading />
{:else if error && !run}
  <p class="center-msg error">{error}</p>
{:else if run && definition}
  {#if error}<div class="banner error" role="alert">{error}</div>{/if}
  <BriefingEdition
    showMetadata={false}
    edition={{
      id: run.id,
      briefing_name: definition.name,
      subject: run.subject ?? definition.title,
      body_md: run.body_md,
      document: run.document ?? null,
      read_at: null,
      generated_at: run.published_at || run.created_at || '',
      task_id: null,
      delivered_to: [],
    }}
  >
    {#snippet meta()}
      {@const at = run!.published_at || run!.created_at || ''}
      <p class="caption edition-meta">
        <span class="group-label"><Users size={13} aria-hidden="true" />{groupLabel}</span>
        <time datetime={at}>{formatDateTime(at, { dateStyle: 'medium', timeStyle: 'short' })}</time>
        <span>Delivered by {channelLabel(definition!.channel)}</span>
        {#if summary}<span>{summary}</span>{/if}
      </p>
    {/snippet}
  </BriefingEdition>
{/if}

<style>
  .group-label {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
  }
</style>
