<script lang="ts">
  import { invalidateNavUnreadCounts } from '$lib/stores/navUnread';
  import { onMount, tick } from 'svelte';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import {
    getBriefingTargets,
    type BriefingTarget,
    getBriefingArchiveItem,
    markBriefingArchiveRead,
    getBriefings,
    getBriefingConfig,
    type BriefingArchiveItem,
  } from '$lib/api';
  import {
    selectedBriefingId,
    selectedGroupEdition,
    briefingDeleteRequest,
    briefingArchiveCount,
    briefingArchiveError,
    briefingReadAck,
  } from '$lib/stores/briefings';
  import GroupEdition from '$lib/components/briefings/GroupEdition.svelte';
  import BriefingEdition from '$lib/components/briefings/BriefingEdition.svelte';
  import { Alert, Button, EmptyState, KebabMenu, LoadState } from '@minot/ui';

  let groupTarget = $state<BriefingTarget | null>(null);
  $effect(() => {
    const group = $selectedGroupEdition?.group;
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
  let readErrorId = $state<number | null>(null);
  let visible = $state(false);
  let mounted = false;
  const pendingReads = new Set<number>();
  const confirmedReads = new Map<number, string>();

  onMount(() => {
    mounted = true;
    const updateVisibility = () => {
      visible = document.visibilityState === 'visible';
    };
    updateVisibility();
    document.addEventListener('visibilitychange', updateVisibility);
    return () => {
      mounted = false;
      document.removeEventListener('visibilitychange', updateVisibility);
    };
  });

  async function acknowledgeRead(id: number, stillPresented = () => true) {
    await tick();
    if (
      !mounted ||
      $selectedGroupEdition ||
      !stillPresented() ||
      document.visibilityState !== 'visible' ||
      $selectedBriefingId !== id ||
      current?.id !== id ||
      loading ||
      error ||
      current.read_at ||
      pendingReads.has(id)
    )
      return;

    const confirmed = confirmedReads.get(id);
    if (confirmed) {
      current = { ...current, read_at: confirmed };
      return;
    }
    pendingReads.add(id);
    readErrorId = null;
    try {
      const result = await markBriefingArchiveRead(id);
      // A reply with no read_at would leave the entry unread and re-arm the
      // presentation effect, so it is a failure rather than a reason to post again.
      if (!result?.read_at) throw new Error('read response carried no read_at');
      invalidateNavUnreadCounts();
      confirmedReads.set(id, result.read_at);
      briefingReadAck.set({ id, read_at: result.read_at });
      if (mounted && $selectedBriefingId === id && current?.id === id) {
        current = { ...current, read_at: result.read_at };
        readErrorId = null;
      }
    } catch {
      if (mounted && $selectedBriefingId === id && current?.id === id) readErrorId = id;
    } finally {
      pendingReads.delete(id);
    }
  }

  // Only a new presentation schedules an attempt. Read errors and pending
  // requests are deliberately outside the effect's dependencies.
  $effect(() => {
    const id = $selectedGroupEdition ? null : $selectedBriefingId;
    if (!visible || id == null || current?.id !== id || loading || error) return;
    let active = true;
    void acknowledgeRead(id, () => active);
    return () => {
      active = false;
    };
  });

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
            (briefing) => briefing.name === name && briefing.id !== undefined,
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
    const id = $selectedGroupEdition ? null : $selectedBriefingId;
    readErrorId = null;
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

<div class="reader card-pane">
  {#if $selectedGroupEdition}
    {#key `${$selectedGroupEdition.group}:${$selectedGroupEdition.run}`}<GroupEdition
        selection={$selectedGroupEdition}
        groupLabel={groupTarget?.label ?? $selectedGroupEdition.group}
      />{/key}
  {:else}
    <LoadState {loading} {error}>
      {#if current}
        {#if readErrorId === current.id && current.id === $selectedBriefingId && !loading}
          <Alert appearance="inline" variant="danger" role="alert">
            Could not mark this briefing as read.
            {#snippet actions()}
              <Button size="sm" onclick={() => acknowledgeRead(current!.id)}>Retry</Button>
            {/snippet}
          </Alert>
        {/if}
        {#key current.id}<BriefingEdition
            edition={current}
            {editableBlockIds}
            onEditBlock={editBlock}
          >
            {#snippet actions()}
              <KebabMenu
                ariaLabel="Briefing actions"
                items={[
                  {
                    label: 'Delete',
                    danger: true,
                    onSelect: () => briefingDeleteRequest.set(current),
                  },
                ]}
              />
            {/snippet}
          </BriefingEdition>{/key}
      {:else}
        <!-- Ahead of the empty state and behind everything else: a briefing already
             on screen stays readable through a failed refresh, but an archive that
             could not be fetched must not read as an archive with nothing in it. -->
        <LoadState loading={$briefingArchiveCount === null} error={$briefingArchiveError}>
          <EmptyState
            title="No briefings yet"
            actionLabel="Set up briefings"
            actionHref="{base}/briefings/settings"
          >
            Once a scheduled briefing runs it will appear here. The schedule and content blocks are
            set up in settings.
          </EmptyState>
        </LoadState>
      {/if}
    </LoadState>
  {/if}
</div>

<style>
  .reader {
    flex: 1 0 auto;
    display: flex;
    flex-direction: column;
    min-width: 0;
  }
</style>
