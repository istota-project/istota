<script lang="ts">
  import { Alert, Icon } from '$lib/components/ui';
  import type { AdminNextcloudCheck } from '$lib/api';
  import { CircleCheck } from '@lucide/svelte';

  interface Props {
    /** The daemon's last row for this connection; null when none is recorded. */
    check: AdminNextcloudCheck | null;
    /** Said when there is no row. */
    empty: string;
    remedyTestid?: string;
  }

  let { check, empty, remedyTestid }: Props = $props();

  let failing = $derived(check !== null && (check.status === 'warn' || check.status === 'fail'));
</script>

<!--
  A doctor row as a connection card shows it: a passing check is one quiet
  line, a failing one a tinted box with its remedy, so a problem is the thing
  the eye lands on and a healthy card stays calm.
-->
{#if check === null}
  <p class="check-line caption">{empty}</p>
{:else if failing}
  <Alert appearance="inline" variant={check.status === 'fail' ? 'danger' : 'warn'}>
    <div class="check-problem">
      <p>{check.detail}</p>
      {#if check.remedy}
        <p class="remedy" data-testid={remedyTestid}>{check.remedy}</p>
      {/if}
    </div>
  </Alert>
{:else}
  <p class="check-line">
    {#if check.status === 'ok'}
      <span class="check-ok" aria-hidden="true"><Icon icon={CircleCheck} size="sm" /></span>
    {/if}
    <span>{check.detail}</span>
  </p>
{/if}

<style>
  .check-line {
    display: flex;
    align-items: flex-start;
    gap: var(--space-2);
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-secondary);
    overflow-wrap: anywhere;
  }

  .check-ok {
    display: inline-flex;
    flex: none;
    margin-top: 0.15em;
    color: var(--status-success-fg);
  }

  .check-problem {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    overflow-wrap: anywhere;
  }

  .check-problem p {
    margin: 0;
  }

  .remedy {
    color: var(--text-primary);
  }
</style>
