<script lang="ts">
  import { base } from '$app/paths';
  import { renderMarkdown } from '$lib/markdown';
  import { getBriefingArchiveItem, type BriefingArchiveItem } from '$lib/api';
  import { selectedBriefingId, briefingArchiveCount } from '$lib/stores/briefings';

  let current = $state<BriefingArchiveItem | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let loadedId = $state<number | null>(null);

  function fmtDate(iso: string): string {
    try {
      return new Date(iso).toLocaleString(undefined, {
        dateStyle: 'medium',
        timeStyle: 'short',
      });
    } catch {
      return iso;
    }
  }

  // Fetch the full briefing (with body) whenever the selection changes.
  $effect(() => {
    const id = $selectedBriefingId;
    if (id == null) {
      current = null;
      loadedId = null;
      return;
    }
    if (id === loadedId) return;
    loadedId = id;
    loading = true;
    error = null;
    getBriefingArchiveItem(id)
      .then((item) => {
        // Guard against an out-of-order response after a fast re-select.
        if ($selectedBriefingId === id) current = item;
      })
      .catch((e) => {
        error = e instanceof Error ? e.message : 'Failed to load briefing';
      })
      .finally(() => {
        loading = false;
      });
  });
</script>

<svelte:head>
  <title>Briefings</title>
</svelte:head>

<div class="reader">
  {#if error}
    <p class="status error">{error}</p>
  {:else if current}
    <article class="briefing">
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
      <div class="body markdown">{@html renderMarkdown(current.body_md ?? '')}</div>
    </article>
  {:else if loading || $briefingArchiveCount === null}
    <p class="center-msg">Loading…</p>
  {:else}
    <div class="empty-state">
      <h1>No briefings yet</h1>
      <p class="muted">
        Once a scheduled briefing runs it will appear here. Set up the schedule and content blocks
        in <a href="{base}/briefings/settings">settings</a>.
      </p>
    </div>
  {/if}
</div>

<style>
  .reader {
    /* flex-basis: auto (not 0) so the box grows with its content — otherwise
		   the box is pinned to the scroll viewport height and long briefings
		   overflow *past* padding-bottom, losing the bottom gap at scroll-end.
		   flex-grow keeps short content (and the empty state) filling the area. */
    flex: 1 0 auto;
    /* A column so the loading state (`.center-msg`, the only child in that
		   branch) can center itself in the pane with `flex: 1`. The article and
		   the empty state keep their natural height at the top of the column. */
    display: flex;
    flex-direction: column;
    padding: 1.5rem 2rem;
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
      padding: 1rem 0.75rem;
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
    margin: 0 0 1rem;
    padding-left: 0;
    list-style-position: inside;
  }

  .body :global(li) {
    margin: 0.25rem 0;
  }

  .briefing-head h1 {
    margin: 0 0 0.25rem;
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
    margin-top: 1.5rem;
  }

  .body :global(table) {
    border-collapse: collapse;
    font-size: var(--text-sm);
  }

  .body :global(th),
  .body :global(td) {
    border: 1px solid var(--border-subtle);
    padding: 0.3rem 0.6rem;
    text-align: left;
  }

  .status {
    padding: 1.5rem 0;
    color: var(--text-dim);
  }

  .status.error {
    color: var(--status-danger-fg);
  }

  .empty-state {
    max-width: 32rem;
  }

  .empty-state h1 {
    font-size: 1.1rem;
    margin: 0 0 0.5rem;
  }

  .muted {
    color: var(--text-muted);
  }
</style>
