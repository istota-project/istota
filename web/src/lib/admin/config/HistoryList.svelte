<script lang="ts">
  import type { ConfigRequest, ConfigRequestStatus } from '$lib/api';
  import { Badge, Button } from '$lib/components/ui';
  import { formatDateTime } from '$lib/dateFormat';

  type BadgeVariant = 'neutral' | 'danger' | 'warn' | 'success' | 'info' | 'partial';

  interface Props {
    /** Newest first; null while loading. */
    requests: ConfigRequest[] | null;
    error: string;
    /** Why an applied document cannot be restored now, or null when it can. */
    restoreBlockedReason: string | null;
    /** The request being restored, while its preview is fetched. */
    restoring: number | null;
    onRestore: (id: number) => void;
  }

  let { requests, error, restoreBlockedReason, restoring, onRestore }: Props = $props();

  const STATUS_VARIANT: Record<ConfigRequestStatus, BadgeVariant> = {
    pending: 'info',
    taken: 'info',
    building: 'info',
    applied: 'success',
    unchanged: 'neutral',
    refused: 'danger',
    failed: 'danger',
    cancelled: 'neutral',
  };

  function summary(request: ConfigRequest): string {
    const parts: string[] = [];
    if (request.changed_sections.length) parts.push(request.changed_sections.join(', '));
    if (request.secret_names.length) parts.push(`credentials: ${request.secret_names.join(', ')}`);
    if (request.admins_changed) parts.push('the admin list');
    return parts.length ? parts.join(' · ') : 'nothing';
  }
</script>

<!--
  Every change made from this page, newest first. Diffs and host output are
  stored redacted, so nothing here holds a credential. Restoring an applied
  change brings back its config.toml through the same review as any other
  change; credentials and the admin list stay as they are now.
-->
<section class="card">
  <h2 class="section-title">History</h2>
  {#if error}
    <p class="form-error">{error}</p>
  {:else if requests === null}
    <p class="caption">Loading…</p>
  {:else if requests.length === 0}
    <p class="caption">No change has been made from this page yet.</p>
  {:else}
    {#if restoreBlockedReason}
      <p class="caption history-note">{restoreBlockedReason}</p>
    {/if}
    <ul class="history">
      {#each requests as request (request.id)}
        <li class="history-row">
          <div class="history-head">
            <span class="history-id">#{request.id}</span>
            <Badge size="sm" variant={STATUS_VARIANT[request.status] ?? 'neutral'}
              >{request.status}</Badge
            >
            <span class="history-meta">
              {request.admin_id} · {formatDateTime(request.finished_at ?? request.created_at)}
            </span>
            {#if request.status === 'applied'}
              <Button
                variant="ghost"
                size="sm"
                disabled={!!restoreBlockedReason || restoring !== null}
                loading={restoring === request.id}
                loadingLabel="Checking…"
                onclick={() => onRestore(request.id)}>Restore</Button
              >
            {/if}
          </div>
          <p class="caption history-summary">{summary(request)}</p>
          {#if request.diff_text || request.output_text}
            <details>
              <summary class="caption">Details</summary>
              {#if request.diff_text}<pre class="history-text">{request.diff_text}</pre>{/if}
              {#if request.output_text}<pre class="history-text">{request.output_text}</pre>{/if}
            </details>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .history {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .history-row {
    padding: var(--space-2) 0;
    border-bottom: 1px solid var(--border-subtle);
  }

  .history-row:last-child {
    border-bottom: none;
  }

  .history-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
  }

  .history-id {
    font-weight: 600;
  }

  .history-meta {
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .history-summary,
  .history-note {
    margin: var(--space-1) 0 0;
  }

  details summary {
    cursor: pointer;
  }

  .history-text {
    margin: var(--space-1) 0 0;
    padding: var(--space-2);
    max-height: 16rem;
    overflow: auto;
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    background: var(--surface-base);
    border-radius: var(--radius-sm);
    white-space: pre;
  }
</style>
