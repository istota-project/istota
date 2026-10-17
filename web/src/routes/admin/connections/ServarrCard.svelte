<script lang="ts">
  import { base } from '$app/paths';
  import type { AdminServarrApp, AdminServarrConnection } from '$lib/api';
  import { Badge, Button, NoticeBanner } from '$lib/components/ui';
  import { formatRelative } from '$lib/dateFormat';
  import { waitForDoctorRun } from '$lib/doctorRun';
  import { notifyError } from '$lib/stores/notices';

  interface Props {
    connection: AdminServarrConnection;
    /** Re-read `/admin/connections` once the daemon has answered a run. */
    onchecked: () => Promise<void> | void;
  }

  let { connection, onchecked }: Props = $props();

  let checking = $state(false);
  let unanswered = $state(false);

  const STATUS = {
    disabled: { variant: 'neutral', label: 'Disabled' },
    incomplete: { variant: 'warn', label: 'Setup incomplete' },
    configured: { variant: 'success', label: 'Configured' },
  } as const;
  const CHECK = {
    ok: { variant: 'success', label: 'OK' },
    warn: { variant: 'warn', label: 'Warning' },
    fail: { variant: 'danger', label: 'Failed' },
    none: { variant: 'neutral', label: 'Not checked' },
  } as const;

  let badge = $derived(STATUS[connection.status]);
  let stale = $derived(connection.apps.some((app) => app.check?.stale));
  // A lasting state rather than an event: every app failed its last check.
  let noneAnswered = $derived(
    connection.apps.length > 0 &&
      connection.apps.every(
        (app) => app.check !== null && (app.check.status === 'warn' || app.check.status === 'fail'),
      ),
  );
  let defaults = $derived(Object.entries(connection.defaults));

  function checkBadge(app: AdminServarrApp) {
    const check = app.check;
    return check === null || check.status === 'skip' ? CHECK.none : CHECK[check.status];
  }

  function showRemedy(app: AdminServarrApp): boolean {
    const check = app.check;
    return (
      check !== null && (check.status === 'warn' || check.status === 'fail') && check.remedy !== ''
    );
  }

  // The URL comes from config and is shown as typed; only an http(s) address
  // becomes a link, so a stray scheme never lands in an href.
  function linkable(url: string): boolean {
    return /^https?:\/\//i.test(url);
  }

  function keyLabel(app: AdminServarrApp): string {
    if (app.api_key_set === null) return 'Unknown';
    return app.api_key_set ? 'Set' : 'Not set';
  }

  function describeDefaults(table: Record<string, string>): string {
    return Object.entries(table)
      .map(([field, value]) => `${field.replace(/_/g, ' ')}: ${value}`)
      .join(' · ');
  }

  async function checkNow() {
    if (checking) return;
    checking = true;
    unanswered = false;
    try {
      const result = await waitForDoctorRun(false);
      if (result.answered) {
        await onchecked();
      } else {
        unanswered = true;
      }
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'Failed to run the check');
    } finally {
      checking = false;
    }
  }
</script>

<section class="card" data-testid="servarr-card">
  <div class="card-head">
    <h2>{connection.label}</h2>
    <span class="status">
      {#if stale}
        <span class="caption">the daemon's last report is old</span>
      {/if}
      <Badge variant={badge.variant}>{badge.label}</Badge>
    </span>
  </div>

  {#if connection.status === 'disabled'}
    <p class="summary">
      This install has no arr apps for the servarr skill: no <code>[servarr.apps]</code> table is set.
    </p>
  {:else}
    {#if noneAnswered}
      <NoticeBanner title="None of the apps passed the last check" variant="warn" />
    {/if}

    <ul class="apps">
      {#each connection.apps as app (app.app)}
        {@const check = app.check}
        <li class="app" data-testid={`servarr-app-${app.app}`}>
          <div class="card-head">
            <h3 class="app-name">{app.app}</h3>
            {#if !app.incomplete}
              <Badge variant={checkBadge(app).variant} size="sm">{checkBadge(app).label}</Badge>
            {/if}
          </div>
          {#if app.incomplete}
            <p class="summary">{app.problem}</p>
          {:else}
            <dl class="kv">
              <dt>URL</dt>
              <dd>
                {#if linkable(app.url)}
                  <a href={app.url} target="_blank" rel="noopener noreferrer">{app.url}</a>
                {:else}
                  {app.url}
                {/if}
              </dd>
              <dt>API key</dt>
              <dd>{keyLabel(app)}</dd>
            </dl>
            {#if app.problem}
              <p class="summary remedy">{app.problem}</p>
            {/if}
            {#if check}
              <p class="summary">{check.detail}</p>
              {#if showRemedy(app)}
                <p class="summary remedy" data-testid={`servarr-remedy-${app.app}`}>
                  {check.remedy}
                </p>
              {/if}
              {#if check.at}
                <p class="caption">checked {formatRelative(check.at)}</p>
              {/if}
            {:else if !app.problem}
              <p class="summary">The daemon has not recorded a check of this app yet.</p>
            {/if}
          {/if}
        </li>
      {/each}
    </ul>

    <dl class="kv">
      {#if connection.missing.length > 0}
        <dt>Not configured</dt>
        <dd>{connection.missing.join(', ')}</dd>
      {/if}
      <dt>Requesters</dt>
      <dd>
        {connection.requesters.length > 0 ? connection.requesters.join(', ') : 'Admins only'}
      </dd>
    </dl>

    {#if defaults.length > 0}
      <h3 class="micro-label">Add defaults, as configured</h3>
      <dl class="kv">
        {#each defaults as [app, table] (app)}
          <dt>{app}</dt>
          <dd>{describeDefaults(table ?? {})}</dd>
        {/each}
      </dl>
    {/if}

    {#if unanswered}
      <p class="caption">
        The daemon has not answered the request yet, so the results shown are from its last run.
        Check that it is running.
      </p>
    {/if}
  {/if}

  <p class="summary">
    Configured is no claim that the keys work; each app's check says that. The web interface never
    calls an arr app.
  </p>
  <div class="form-actions">
    <Button variant="secondary" href={`${base}/admin/config?section=servarr`}>Configure</Button>
    {#if connection.apps.length > 0}
      <Button variant="secondary" onclick={checkNow} loading={checking} loadingLabel="Checking…"
        >Check now</Button
      >
    {/if}
  </div>
</section>

<style>
  .status {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
  }

  .apps {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .app {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .app-name {
    margin: 0;
    text-transform: capitalize;
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

  dd,
  code {
    overflow-wrap: anywhere;
    user-select: text;
  }
</style>
