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

  let { selection, groupLabel }: { selection: GroupEditionSelection; groupLabel: string } =
    $props();

  let definition = $state<GroupBriefingDefinition | null>(null);
  let run = $state<GroupBriefingRun | null>(null);
  let delivery = $state<BriefingDelivery | null>(null);
  let loading = $state(true);
  let error = $state('');

  const CHANNEL_LABELS: Record<string, string> = {
    in_app: 'In app',
    email: 'Email',
    talk: 'Talk',
    ntfy: 'ntfy',
  };
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
      <p class="caption group-meta">
        <Users size={14} aria-hidden="true" />
        <span
          >{groupLabel} · {CHANNEL_LABELS[definition!.channel] ?? definition!.channel} · {formatDateTime(
            run!.published_at || run!.created_at || '',
            { dateStyle: 'medium', timeStyle: 'short' },
          )}</span
        >
        {#if summary}<span>· {summary}</span>{/if}
      </p>
    {/snippet}
  </BriefingEdition>
{/if}

<style>
  .group-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1);
    margin: 0;
  }
</style>
