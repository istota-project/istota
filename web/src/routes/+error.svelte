<script lang="ts">
  import { base } from '$app/paths';
  import { page } from '$app/state';
  import { CircleAlert, House, Lock, RotateCw, SearchX, ServerCrash } from 'lucide-svelte';

  const status = $derived(page.status ?? 500);
  const isServer = $derived(status >= 500);
  const isMissing = $derived(status === 404);
  const isForbidden = $derived(status === 401 || status === 403);

  const title = $derived(
    isMissing
      ? 'Page not found'
      : isForbidden
        ? "You don't have access"
        : isServer
          ? 'Something went wrong'
          : 'That request failed',
  );

  const detail = $derived(
    isMissing
      ? 'This page does not exist, or it moved somewhere else.'
      : isForbidden
        ? 'Your account is not permitted to view this page. Signing in again may help.'
        : isServer
          ? 'The app hit an internal error. Reloading usually clears it; if it keeps happening the scheduler log will say why.'
          : 'The app could not complete that request.',
  );

  /* SvelteKit fills in a generic message ("Internal Error", "Not Found") when a
     load function throws without one. Echoing that under a heading that already
     says the same thing is noise — only show the message when it carries
     something the heading doesn't. */
  const genericMessages = new Set(['Internal Error', 'Not Found', 'Forbidden', 'Unauthorized']);
  const message = $derived.by(() => {
    const raw = page.error?.message?.trim();
    if (!raw || genericMessages.has(raw)) return '';
    return raw;
  });

  const severity = $derived(isServer ? 'danger' : isForbidden ? 'warn' : 'neutral');
</script>

<div class="error-page">
  <div class="error-inner">
    <div class="error-badge severity-{severity}">
      {#if isMissing}
        <SearchX size={26} strokeWidth={1.5} />
      {:else if isForbidden}
        <Lock size={26} strokeWidth={1.5} />
      {:else if isServer}
        <ServerCrash size={26} strokeWidth={1.5} />
      {:else}
        <CircleAlert size={26} strokeWidth={1.5} />
      {/if}
    </div>

    <p class="error-code">Error {status}</p>
    <h1>{title}</h1>
    <p class="error-detail">{detail}</p>

    {#if message}
      <p class="error-message">{message}</p>
    {/if}

    <div class="error-actions">
      {#if isServer}
        <button type="button" class="action primary" onclick={() => location.reload()}>
          <RotateCw size={14} />
          Reload
        </button>
      {/if}
      <a href="{base}/" class="action" class:primary={!isServer}>
        <House size={14} />
        Go to dashboard
      </a>
    </div>
  </div>
</div>

<style>
  /* The error page renders inside `main.app-content`, which comes in two shapes:
     the six full-height routes carry `.app-content-fill` (a flex column that
     grows and strips the page padding), and the rest are plain padded blocks
     that size to their content. Left alone that makes one error page centred in
     the viewport and the other pinned under the nav with an extra 1.5rem of
     inherited padding — the Admin-vs-Briefings difference. So flatten main to
     one shape whenever it is holding this page, and let the page carry its own
     padding. Same specificity as `.app-content.app-content-fill`, and it sets
     the same values, so the tie between them is inconsequential. */
  :global(.app-content:has(> .error-page)) {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
    padding: 0;
  }

  /* `min-height` is the fallback for the (modern-browser-only) `:has()` above —
     without it a padded route would collapse to content height. The parent may
     be `overflow: hidden` on a fill route, so this scrolls internally rather
     than relying on the document; centring is `margin: auto` on the inner block
     instead of `justify-content`, which would clip the top when content
     overflows. */
  .error-page {
    flex: 1;
    min-height: 60vh;
    display: flex;
    overflow-y: auto;
    padding: 2rem max(1.5rem, var(--safe-left)) max(2rem, var(--safe-bottom))
      max(1.5rem, var(--safe-right));
  }

  .error-inner {
    margin: auto;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-2);
    text-align: center;
  }

  .error-badge {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 3.75rem;
    height: 3.75rem;
    margin-bottom: var(--space-4);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-pill);
    background: var(--surface-card);
    color: var(--text-muted);
  }

  .severity-danger {
    border-color: var(--status-danger-bg);
    background: var(--status-danger-bg);
    color: var(--status-danger-fg);
  }

  .severity-warn {
    border-color: var(--status-warn-bg);
    background: var(--status-warn-bg);
    color: var(--status-warn-fg);
  }

  .error-code {
    margin: 0;
    font-size: var(--text-xs);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text-dim);
  }

  h1 {
    margin: 0;
    font-size: 1.35rem;
    font-weight: 500;
    color: var(--text-primary);
  }

  .error-detail {
    margin: 0.15rem 0 0;
    max-width: 34rem;
    font-size: var(--text-base);
    line-height: 1.5;
    color: var(--text-muted);
    text-wrap: pretty;
  }

  /* The thrown message, when it says more than the heading. Monospace and boxed
     so it reads as diagnostic output rather than as prose addressed to the user. */
  .error-message {
    margin: var(--space-4) 0 0;
    max-width: 34rem;
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-card);
    background: var(--surface-card);
    font-family: ui-monospace, monospace;
    font-size: var(--text-sm);
    line-height: 1.45;
    color: var(--text-secondary);
    /* A message can be a URL or an unbroken token; keep it inside the box. */
    overflow-wrap: anywhere;
  }

  .error-actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: var(--space-2);
    margin-top: 1.4rem;
  }

  .action {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-pill);
    background: var(--surface-card);
    color: var(--text-muted);
    font: inherit;
    font-size: var(--text-base);
    line-height: 1.2;
    text-decoration: none;
    cursor: pointer;
    transition:
      color var(--transition-fast),
      background var(--transition-fast),
      border-color var(--transition-fast);
  }

  .action:hover {
    background: var(--surface-raised);
    color: var(--text-primary);
  }

  /* The bot's identity accent, not the interactive blue — this is the one
     prominent control on an otherwise empty page, so it should read as the
     app's own rather than as a generic form button. The `-fill` token, not
     --accent-amber: see app.css for why the light theme's darker amber is the
     wrong value under text. */
  .action.primary {
    border-color: transparent;
    background: var(--accent-amber-fill);
    color: var(--accent-amber-fill-fg);
  }

  .action.primary:hover {
    background: var(--accent-amber-fill-hover);
    color: var(--accent-amber-fill-fg);
  }
</style>
