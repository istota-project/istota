<script lang="ts">
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import {
    getBriefingTargets,
    type BriefingTarget,
    getBriefingArchiveItem,
    getBriefings,
    getBriefingConfig,
    type BriefingArchiveItem,
  } from '$lib/api';
  import {
    selectedBriefingId,
    selectedBriefingGroup,
    briefingArchiveCount,
    briefingArchiveError,
  } from '$lib/stores/briefings';
  import GroupBriefings from '$lib/components/briefings/GroupBriefings.svelte';
  import BriefingEdition from '$lib/components/briefings/BriefingEdition.svelte';
  import { EmptyState, PageLoading } from '$lib/components/ui';

  let groupTarget = $state<BriefingTarget | null>(null);
  $effect(() => {
    const group = $selectedBriefingGroup;
    groupTarget = null;
    if (!group) return;
    let active = true;
    getBriefingTargets()
      .then((response) => {
        if (active)
          groupTarget = response.targets.find((target) => target.group_id === group) ?? null;
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  });
  let current = $state<BriefingArchiveItem | null>(null);
  let editableBlockIds = $state<number[]>([]);
  let loading = $state(false);
  let error = $state<string | null>(null);

  $effect(() => {
    const name = current?.briefing_name;
    editableBlockIds = [];
    if (!name || !current?.document) return;
    let active = true;
    // Live definitions govern editing only. Archived content stays immutable.
    Promise.all([getBriefings(), getBriefingConfig()])
      .then(([schedules, config]) => {
        if (
          !active ||
          !schedules?.briefings.some(
            (briefing) => briefing.name === name && briefing.managed === 'db',
          )
        )
          return;
        editableBlockIds =
          config?.briefings
            .find((briefing) => briefing.name === name)
            ?.blocks.map((block) => block.id) ?? [];
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  });

  function editBlock(blockId: number) {
    if (!current) return;
    void goto(
      `${base}/briefings/settings?name=${encodeURIComponent(current.briefing_name)}&block=${blockId}`,
    );
  }

  // Fetch the full briefing (with body) whenever the selection changes.
  $effect(() => {
    const id = $selectedBriefingGroup ? null : $selectedBriefingId;
    if (id == null) {
      current = null;
      loading = false;
      error = null;
      return;
    }
    current = null;
    loading = true;
    error = null;
    let active = true;
    getBriefingArchiveItem(id)
      .then((item) => {
        // Guard against an out-of-order response after a fast re-select.
        if (active && $selectedBriefingId === id) current = item;
      })
      .catch((e) => {
        if (!active || $selectedBriefingId !== id) return;
        error = e instanceof Error ? e.message : 'Failed to load briefing';
      })
      .finally(() => {
        if (active && $selectedBriefingId === id) loading = false;
      });
    return () => {
      active = false;
    };
  });
</script>

<svelte:head>
  <title>Briefings</title>
</svelte:head>

<div class="content-frame reader">
  {#if $selectedBriefingGroup}
    {#key $selectedBriefingGroup}<GroupBriefings
        groupId={$selectedBriefingGroup}
        label={groupTarget?.label ?? 'Group'}
        canCreate={groupTarget?.capabilities.author_briefings ?? false}
      />{/key}
  {:else if loading}
    <PageLoading />
  {:else if error}
    <p class="center-msg error">{error}</p>
  {:else if current}
    <p class="caption">Just me</p>
    {#key current.id}<BriefingEdition
        edition={current}
        {editableBlockIds}
        onEditBlock={editBlock}
      />{/key}
  {:else if $briefingArchiveCount === null}
    <PageLoading />
  {:else if $briefingArchiveError}
    <!-- Ahead of the empty state and behind everything else: a briefing already
         on screen stays readable through a failed refresh, but an archive that
         could not be fetched must not read as an archive with nothing in it. -->
    <p class="center-msg error">{$briefingArchiveError}</p>
  {:else}
    <EmptyState
      title="No briefings yet"
      actionLabel="Set up briefings"
      actionHref="{base}/briefings/settings"
    >
      Once a scheduled briefing runs it will appear here. The schedule and content blocks are set up
      in settings.
    </EmptyState>
  {/if}
</div>

<style>
  .reader {
    flex: 1 0 auto;
    display: flex;
    flex-direction: column;
    min-width: 0;
    /* design-lint-allow: match the shared settings container's bottom spacing. */
    padding: var(--space-6) var(--space-4) 4rem;
    padding-bottom: max(4rem, var(--safe-bottom));
  }

  @media (max-width: 768px) {
    .reader {
      /* design-lint-allow: match the shared settings container's mobile bottom spacing. */
      padding: var(--space-4) var(--space-3) 3rem;
      padding-bottom: max(3rem, var(--safe-bottom));
    }
  }
  @media (max-width: 640px) {
    .reader {
      padding-top: var(--space-3);
    }
  }
</style>
