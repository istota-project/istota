<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { Newspaper } from '@lucide/svelte';
  import { Button, EmptyState } from '$lib/components/ui';
  import { SettingsCard } from '$lib/components/settings';
  import { getGroupBriefings, type GroupBriefingDefinition } from '$lib/api';
  import { formatDateTime } from '$lib/dateFormat';

  // An index into Briefings, which owns reading, editing and delivery choices.
  let { groupId, canAuthor }: { groupId: string; canAuthor: boolean } = $props();
  let items = $state<GroupBriefingDefinition[]>([]);
  let loaded = $state(false);
  let failed = $state(false);

  const CHANNEL_LABELS: Record<string, string> = {
    in_app: 'In app',
    email: 'Email',
    talk: 'Talk',
    ntfy: 'ntfy',
  };
  const q = encodeURIComponent;
  const settingsUrl = (name?: string) =>
    `${base}/briefings/settings/?group=${q(groupId)}&${name ? `briefing=${q(name)}` : 'new=1'}`;

  function readUrl(item: GroupBriefingDefinition): string | null {
    const last = item.last_run;
    if (!last || last.state !== 'published') return null;
    return `${base}/briefings/?group=${q(groupId)}&briefing=${q(item.name)}&run=${last.id}`;
  }

  function status(item: GroupBriefingDefinition): string {
    const channel = CHANNEL_LABELS[item.channel] ?? item.channel;
    if (!item.enabled) return `${channel} · Draft`;
    const last = item.last_run?.published_at;
    return last
      ? `${channel} · Last ${formatDateTime(last, { dateStyle: 'medium' })}`
      : `${channel} · No editions yet`;
  }

  onMount(() => {
    let active = true;
    getGroupBriefings(groupId)
      .then((result) => {
        if (active) items = result.items;
      })
      .catch(() => {
        if (active) failed = true;
      })
      .finally(() => {
        if (active) loaded = true;
      });
    return () => {
      active = false;
    };
  });
</script>

<SettingsCard title="Briefings" description="Briefings generated once and shared with this group.">
  {#snippet actions()}
    {#if canAuthor}<Button size="sm" href={settingsUrl()}>New briefing</Button>{/if}
  {/snippet}
  {#if failed}
    <p class="caption">Could not load this group's briefings.</p>
  {:else if loaded && !items.length}
    <EmptyState size="sm">No briefings for this group yet.</EmptyState>
  {:else}
    <ul class="briefings">
      {#each items as item (item.id)}
        {@const href = readUrl(item)}
        <li>
          <span class="entry-icon" aria-hidden="true"><Newspaper size={16} /></span>
          <span class="text">
            {#if href}<a class="name" {href}>{item.title || item.name}</a>{:else}<span class="name"
                >{item.title || item.name}</span
              >{/if}
            <span class="caption">{status(item)}</span>
          </span>
          {#if canAuthor}<a class="edit caption" href={settingsUrl(item.name)}>Edit</a>{/if}
        </li>
      {/each}
    </ul>
  {/if}
</SettingsCard>

<style>
  .briefings {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .briefings li {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding-block: var(--space-2);
    border-bottom: 1px solid var(--border-subtle);
  }
  .briefings li:last-child {
    border-bottom: none;
  }
  .entry-icon {
    display: flex;
    flex-shrink: 0;
    color: var(--accent-amber);
  }
  .text {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
  }
  .name {
    color: var(--text-primary);
    text-decoration: none;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  a.name:hover,
  .edit:hover {
    text-decoration: underline;
  }
  .edit {
    flex-shrink: 0;
    text-decoration: none;
  }
</style>
