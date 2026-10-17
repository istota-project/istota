<script lang="ts">
  import { base } from '$app/paths';
  import type { AdminNextcloudConnection } from '$lib/api';
  import { Button } from '$lib/components/ui';
  import { formatRelative } from '$lib/dateFormat';
  import { waitForDoctorRun } from '$lib/doctorRun';
  import { Cloud } from '@lucide/svelte';
  import CheckResult from './CheckResult.svelte';
  import ConnectionCard from './ConnectionCard.svelte';

  interface Props {
    connection: AdminNextcloudConnection;
    /** Re-read `/admin/connections` once the daemon has answered a run. */
    onchecked: () => Promise<void> | void;
  }

  let { connection, onchecked }: Props = $props();

  let checking = $state(false);
  let notice = $state('');
  let error = $state('');

  const BADGE = {
    ok: { variant: 'success', label: 'OK' },
    warn: { variant: 'warn', label: 'Warning' },
    fail: { variant: 'danger', label: 'Failed' },
    none: { variant: 'neutral', label: 'Not checked' },
  } as const;

  let check = $derived(connection.check);
  let badge = $derived(
    !connection.configured
      ? null
      : check === null || check.status === 'skip'
        ? BADGE.none
        : BADGE[check.status],
  );
  // The URL comes from config and is shown as typed; only an http(s) address
  // becomes a link, so a stray scheme never lands in an href.
  let linkable = $derived(/^https?:\/\//i.test(connection.url));

  async function checkNow() {
    if (checking) return;
    checking = true;
    notice = '';
    error = '';
    try {
      const result = await waitForDoctorRun(false);
      if (result.answered) {
        await onchecked();
      } else {
        notice = 'The daemon has not answered the request yet. Check that it is running.';
      }
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to run the check';
    } finally {
      checking = false;
    }
  }
</script>

<ConnectionCard
  testid="nextcloud-card"
  title={connection.label}
  subtitle="Files, Talk and calendars, as the bot account"
  icon={Cloud}
  {badge}
>
  {#if connection.configured}
    <div class="panel">
      <h3 class="micro-label">Account</h3>
      <dl class="kv">
        <dt>Server</dt>
        <dd>
          {#if linkable}
            <a href={connection.url} target="_blank" rel="noopener noreferrer">{connection.url}</a>
          {:else}
            {connection.url}
          {/if}
        </dd>
        <dt>Bot account</dt>
        <dd>{connection.username || '—'}</dd>
        <dt>Talk</dt>
        <dd>
          {connection.talk_enabled ? `On, as ${connection.talk_bot_username || '—'}` : 'Off'}
        </dd>
        <dt>Storage</dt>
        <dd>
          {connection.storage === 'nextcloud'
            ? 'Nextcloud files, through the mount'
            : 'Local workspace'}
        </dd>
      </dl>
    </div>

    <div class="panel">
      <h3 class="micro-label">Last check</h3>
      <CheckResult
        {check}
        empty="The daemon has not recorded a check of this connection yet."
        remedyTestid="nextcloud-remedy"
      />
      {#if check?.at}
        <p class="caption stamp">checked {formatRelative(check.at)}</p>
      {/if}
      {#if check?.stale}
        <p class="stale">the daemon's last report is old</p>
      {/if}
    </div>

    {#if error}
      <div class="banner error wide">{error}</div>
    {/if}
    {#if notice}
      <div class="banner warn wide">{notice}</div>
    {/if}
  {:else}
    <p class="summary wide">
      This install has no Nextcloud: <code>[nextcloud] url</code> is not set, and the workspace is local.
    </p>
  {/if}

  {#snippet actions()}
    <Button variant="secondary" href={`${base}/admin/config`}>Configure</Button>
    {#if connection.configured}
      <Button variant="secondary" onclick={checkNow} loading={checking} loadingLabel="Checking…"
        >Check now</Button
      >
    {/if}
  {/snippet}
</ConnectionCard>

<style>
  .summary {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }

  .stamp {
    margin: 0;
  }

  .stale {
    margin: 0;
    font-size: var(--text-xs);
    color: var(--status-warn-fg);
  }
</style>
