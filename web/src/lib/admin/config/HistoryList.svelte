<script lang="ts">
  import type { ConfigRequest, ConfigRequestStatus } from '$lib/api';
  import { Badge, Button } from '$lib/components/ui';
  import { formatDateTime } from '$lib/dateFormat';
  import DiffBlock from './DiffBlock.svelte';

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
  <header class="section-header"><h2>History</h2></header>
  {#if error}
    <p class="form-error">{error}</p>
  {:else if requests === null}
    <p class="caption">Loading…</p>
  {:else if requests.length === 0}
    <p class="caption">No change has been made from this page yet.</p>
  {:else}
    {#if restoreBlockedReason}
      <p class="caption">{restoreBlockedReason}</p>
    {/if}
    <ul class="history">
      {#each requests as request (request.id)}
        <li class="history-row">
          <div class="card-head history-head control-row">
            <div class="history-title">
              <span class="history-id">#{request.id}</span>
              <Badge size="sm" variant={STATUS_VARIANT[request.status] ?? 'neutral'}
                >{request.status}</Badge
              >
              <span class="history-summary">{summary(request)}</span>
            </div>
            {#if request.status === 'applied'}
              <Button
                variant="secondary"
                size="sm"
                disabled={!!restoreBlockedReason || restoring !== null}
                loading={restoring === request.id}
                loadingLabel="Checking…"
                onclick={() => onRestore(request.id)}>Restore</Button
              >
            {/if}
          </div>
          <p class="caption">
            {request.admin_id} · {formatDateTime(request.finished_at ?? request.created_at)}
          </p>
          {#if request.diff_text || request.output_text}
            <details class="history-details">
              <summary class="caption">Details</summary>
              <div class="history-body">
                {#if request.diff_text}<DiffBlock lines={request.diff_text.split('\n')} />{/if}
                {#if request.output_text}
                  <div>
                    <span class="micro-label">Host</span>
                    <pre class="history-output">{request.output_text}</pre>
                  </div>
                {/if}
              </div>
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
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    padding: var(--space-3) 0;
    border-top: 1px solid var(--border-subtle);
  }

  .history-row:first-child {
    padding-top: 0;
    border-top: none;
  }

  .history-row:last-child {
    padding-bottom: 0;
  }

  .history-head {
    align-items: center;
  }

  .history-title {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    min-width: 0;
    font-size: var(--text-sm);
  }

  .history-id {
    font-weight: 600;
  }

  .history-summary {
    color: var(--text-secondary);
    overflow-wrap: anywhere;
  }

  .history-details summary {
    cursor: pointer;
    width: fit-content;
  }

  .history-body {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    margin-top: var(--space-2);
  }

  .history-output {
    margin: var(--space-1) 0 0;
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    color: var(--text-muted);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
</style>
