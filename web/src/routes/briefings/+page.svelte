<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { fileLinks } from '$lib/fileViewer/links';
  import { base } from '$app/paths';
  import { renderMarkdown } from '$lib/markdown';
  import {
    getBriefingArchiveItem,
    markBriefingArchiveRead,
    type BriefingArchiveItem,
  } from '$lib/api';
  import {
    selectedBriefingId,
    briefingArchiveCount,
    briefingArchiveError,
  } from '$lib/stores/briefings';
  import { formatDateTime } from '$lib/dateFormat';
  import { Button, EmptyState, PageLoading } from '$lib/components/ui';

  let current = $state<BriefingArchiveItem | null>(null);
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
      confirmedReads.set(id, result.read_at);
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
    const id = $selectedBriefingId;
    if (!visible || id == null || current?.id !== id || loading || error) return;
    let active = true;
    void acknowledgeRead(id, () => active);
    return () => {
      active = false;
    };
  });

  const fmtDate = (iso: string) => formatDateTime(iso, { dateStyle: 'medium', timeStyle: 'short' });

  // Fetch the full briefing (with body) whenever the selection changes.
  $effect(() => {
    const id = $selectedBriefingId;
    readErrorId = null;
    if (id == null) {
      current = null;
      loading = false;
      error = null;
      return;
    }
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
  {#if error}
    <p class="center-msg error">{error}</p>
  {:else if current}
    <article class="briefing">
      {#if readErrorId === current.id && current.id === $selectedBriefingId && !loading}
        <div class="banner error" role="alert">
          Could not mark this briefing as read.
          <Button size="sm" onclick={() => acknowledgeRead(current!.id)}>Retry</Button>
        </div>
      {/if}
      <header class="briefing-head">
        <h1>{current.subject || current.briefing_name}</h1>
        <p class="meta">
          {fmtDate(current.generated_at)}
          {#if current.delivered_to?.length}
            · delivered to {current.delivered_to.join(', ')}
          {/if}
        </p>
      </header>
      <!-- eslint-disable-next-line svelte/no-at-html-tags -->
      <!-- `markdown` is the shared rendered-prose block in app.css (same class
           chat's message body uses). The local rules below layer on top of it —
           Svelte's scoping class outranks the global ones — and only cover where
           a reading surface genuinely differs from a chat bubble (flush lists,
           larger headings). -->
      <div use:fileLinks class="body markdown">{@html renderMarkdown(current.body_md ?? '')}</div>
    </article>
  {:else if loading || $briefingArchiveCount === null}
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
    /* flex-basis: auto (not 0) so the box grows with its content — otherwise
		   the box is pinned to the scroll viewport height and long briefings
		   overflow *past* padding-bottom, losing the bottom gap at scroll-end.
		   flex-grow keeps short content (and the empty state) filling the area. */
    flex: 1 0 auto;
    /* A column so the whole-pane states (`.center-msg`, the only child in the
		   loading and error branches) can center themselves in the pane with
		   `flex: 1`. The article and the empty state keep their natural height at
		   the top of the column. */
    display: flex;
    flex-direction: column;
    padding: var(--space-6) var(--space-8);
    /* The shell hands this route the bottom safe area (insetBottom={onSettings}),
		   so the fill runs to the screen edge and the text clears the home
		   indicator — rather than the fill stopping short and leaving a band of the
		   shell background below the card. Inert where the inset is 0. */
    padding-bottom: max(1.5rem, var(--safe-bottom));
    /* Reading surface, shared with the chat transcript: card-colored in dark,
	     pure white in light. */
    background: var(--surface-reading);
  }

  /* Phone: the desktop 2rem inline gutter costs a fifth of a narrow screen's
	   width, and it left the briefing text inset well past every other landmark
	   on the page. Drop to the 0.75rem the app bar and ShellHeader use, so the
	   body copy lines up with the titles above it. */
  @media (max-width: 768px) {
    .reader {
      padding: var(--space-4) var(--space-3);
      padding-bottom: max(1rem, var(--safe-bottom));
    }
  }

  .briefing {
    max-width: 46rem;
  }

  /* Match the chat message body: same font size (configurable later) and
	   line height so the reader reads at one scale with the rest of the app. */
  .body {
    font-size: var(--text-base);
    line-height: 1.5;
  }

  /* Bullet/numbered lists sit flush with paragraphs — no browser default
	   indent; the marker aligns with the left edge of the surrounding text. */
  .body :global(ul),
  .body :global(ol) {
    margin: 0 0 var(--space-4);
    padding-left: 0;
    list-style-position: inside;
  }

  .body :global(li) {
    margin: var(--space-1) 0;
  }

  .briefing-head h1 {
    margin: 0 0 var(--space-1);
    font-size: 1.25rem;
  }

  .meta {
    margin: 0 0 1.25rem;
    font-size: var(--text-sm);
    color: var(--text-dim);
  }

  .body :global(h1),
  .body :global(h2) {
    font-size: 1.05rem;
    margin-top: var(--space-6);
  }

  .body :global(table) {
    border-collapse: collapse;
    font-size: var(--text-sm);
  }

  .body :global(th),
  .body :global(td) {
    border: 1px solid var(--border-subtle);
    padding: var(--space-1) var(--space-2);
    text-align: left;
  }
</style>
