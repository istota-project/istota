<script lang="ts">
  import { base } from '$app/paths';
  import { getBriefingArchiveItem, type BriefingArchiveItem } from '$lib/api';
  import {
    selectedBriefingId,
    briefingArchiveCount,
    briefingArchiveError,
  } from '$lib/stores/briefings';
  import BriefingEdition from '$lib/components/briefings/BriefingEdition.svelte';
  import { EmptyState, PageLoading } from '$lib/components/ui';

  let current = $state<BriefingArchiveItem | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);

  // Fetch the full briefing (with body) whenever the selection changes.
  $effect(() => {
    const id = $selectedBriefingId;
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

<div class="reader">
  {#if loading}
    <PageLoading />
  {:else if error}
    <p class="center-msg error">{error}</p>
  {:else if current}
    {#key current.id}<BriefingEdition edition={current} />{/key}
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
    padding: var(--space-6) var(--space-8);
    padding-bottom: max(var(--space-6), var(--safe-bottom));
  }

  @media (max-width: 768px) {
    .reader {
      padding: var(--space-4) var(--space-3);
      padding-bottom: max(var(--space-4), var(--safe-bottom));
    }
  }
</style>
