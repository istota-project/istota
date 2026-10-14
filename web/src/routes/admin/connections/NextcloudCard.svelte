<script lang="ts">
  import { base } from '$app/paths';
  import type { AdminNextcloudConnection } from '$lib/api';
  import { Badge, Button } from '$lib/components/ui';
  import { formatRelative } from '$lib/dateFormat';
  import { waitForDoctorRun } from '$lib/doctorRun';

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
    check === null || check.status === 'skip' ? BADGE.none : BADGE[check.status],
  );
  let showRemedy = $derived(
    check !== null && (check.status === 'warn' || check.status === 'fail') && check.remedy !== '',
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

<section class="card" data-testid="nextcloud-card">
  <div class="card-head">
    <h2>{connection.label}</h2>
    {#if connection.configured}
      <span class="status">
        {#if check?.stale}
          <span class="caption">the daemon's last report is old</span>
        {/if}
        <Badge variant={badge.variant}>{badge.label}</Badge>
      </span>
    {/if}
  </div>

  {#if connection.configured}
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

    {#if check}
      <p class="summary">{check.detail}</p>
      {#if showRemedy}
        <p class="summary remedy" data-testid="nextcloud-remedy">{check.remedy}</p>
      {/if}
      {#if check.at}
        <p class="caption">checked {formatRelative(check.at)}</p>
      {/if}
    {:else}
      <p class="summary">The daemon has not recorded a check of this connection yet.</p>
    {/if}

    {#if error}
      <div class="banner error">{error}</div>
    {/if}
    {#if notice}
      <div class="banner warn">{notice}</div>
    {/if}

    <div class="form-actions">
      <Button variant="ghost" href={`${base}/admin/config`}>Change in Configuration</Button>
      <Button variant="secondary" onclick={checkNow} loading={checking} loadingLabel="Checking…"
        >Check now</Button
      >
    </div>
  {:else}
    <p class="summary">
      This install has no Nextcloud: <code>[nextcloud] url</code> is not set, and the workspace is local.
    </p>
    <div class="form-actions">
      <Button variant="ghost" href={`${base}/admin/config`}>Change in Configuration</Button>
    </div>
  {/if}
</section>

<style>
  .status {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
  }

  .summary {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }

  .remedy {
    color: var(--text-primary);
  }

  .caption {
    margin: 0;
  }
</style>
