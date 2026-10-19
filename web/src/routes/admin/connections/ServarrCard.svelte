<script lang="ts">
  import { base } from '$app/paths';
  import type { AdminServarrApp, AdminServarrConnection } from '$lib/api';
  import { Badge, Button, EmptyState, NoticeBanner } from '$lib/components/ui';
  import { formatRelative } from '$lib/dateFormat';
  import { waitForDoctorRun } from '$lib/doctorRun';
  import { notifyError } from '$lib/stores/notices';
  import { Clapperboard } from '@lucide/svelte';
  import CheckResult from './CheckResult.svelte';
  import ConnectionCard from './ConnectionCard.svelte';

  interface Props {
    connection: AdminServarrConnection;
    /** Re-read `/admin/connections` after a check run. */
    onchecked: () => Promise<void> | void;
  }

  let { connection, onchecked }: Props = $props();

  let checking = $state(false);
  let unanswered = $state(false);

  /** The card shows status; `[servarr]` is edited in Configuration. */
  const configureHref = `${base}/admin/config?section=servarr`;

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

<ConnectionCard
  testid="servarr-card"
  title={connection.label}
  subtitle="Sonarr, Radarr, Lidarr and Prowlarr, for the servarr skill"
  icon={Clapperboard}
  {badge}
>
  {#if connection.status === 'disabled'}
    <div class="wide" data-testid="servarr-empty">
      <EmptyState actionLabel="Add apps" actionHref={configureHref}>
        No arr apps are connected. Add Sonarr, Radarr, Lidarr or Prowlarr so the servarr skill can
        search for and request media.
      </EmptyState>
    </div>
  {:else}
    {#if noneAnswered}
      <div class="wide">
        <NoticeBanner title="None of the apps passed the last check" variant="warn" />
      </div>
    {/if}

    <!-- One tile per app, configured or not: a missing app is a dashed tile
         in its place rather than a name in a list further down. -->
    <ul class="apps wide">
      {#each connection.apps as app (app.app)}
        <li class="app" data-testid={`servarr-app-${app.app}`}>
          <div class="app-head">
            <h3 class="app-name title-card">{app.app}</h3>
            {#if !app.incomplete}
              <Badge variant={checkBadge(app).variant} size="sm">{checkBadge(app).label}</Badge>
            {/if}
          </div>
          {#if app.incomplete}
            <p class="banner warn app-problem">{app.problem}</p>
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
              <p class="banner warn app-problem">{app.problem}</p>
            {/if}
            {#if app.check || !app.problem}
              <CheckResult
                check={app.check}
                empty="The daemon has not recorded a check of this app yet."
                remedyTestid={`servarr-remedy-${app.app}`}
              />
            {/if}
            {#if app.check?.at}
              <p class="caption app-foot">checked {formatRelative(app.check.at)}</p>
            {/if}
          {/if}
        </li>
      {/each}
      {#each connection.missing as app (app)}
        <li class="app app-missing" data-testid={`servarr-missing-${app}`}>
          <h3 class="app-name title-card">{app}</h3>
          <p class="caption">Not configured</p>
        </li>
      {/each}
    </ul>

    <div class="panel">
      <h3 class="micro-label">Who can request</h3>
      <p class="value">
        {connection.requesters.length > 0 ? connection.requesters.join(', ') : 'Admins only'}
      </p>
    </div>

    {#if defaults.length > 0}
      <div class="panel">
        <h3 class="micro-label">Add defaults</h3>
        <dl class="kv">
          {#each defaults as [app, table] (app)}
            <dt class="app-label">{app}</dt>
            <dd>{describeDefaults(table ?? {})}</dd>
          {/each}
        </dl>
      </div>
    {/if}

    {#if unanswered}
      <p class="banner warn wide">
        The daemon has not answered the request yet, so the results shown are from its last run.
        Check that it is running.
      </p>
    {/if}
  {/if}

  {#snippet meta()}
    {#if connection.status !== 'disabled'}
      {#if stale}
        <span class="stale">the daemon's last report is old</span>
      {/if}
      <p class="caption">
        Configured is no claim that the keys work; each app's check says that. The web interface
        never calls an arr app.
      </p>
    {/if}
  {/snippet}

  {#snippet actions()}
    <Button
      variant="secondary"
      href="https://istota.cynium.com/docs/features/servarr/"
      target="_blank"
      rel="noopener noreferrer">Setup guide</Button
    >
    <Button variant="secondary" href={configureHref}>Configure</Button>
    {#if connection.apps.length > 0}
      <Button variant="secondary" onclick={checkNow} loading={checking} loadingLabel="Checking…"
        >Check now</Button
      >
    {/if}
  {/snippet}
</ConnectionCard>

<style>
  .caption {
    margin: 0;
  }

  .stale {
    font-size: var(--text-xs);
    color: var(--status-warn-fg);
  }

  /* One tile per app, as many across as the card is wide. */
  .apps {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 14rem), 1fr));
    gap: var(--space-3);
  }

  .app {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    min-width: 0;
    padding: var(--space-3);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--surface-raised);
  }

  /* The stamp sits on each tile's bottom edge, so a row of tiles reads level
     whichever of them carries a remedy. */
  .app-foot {
    margin-top: auto;
  }

  .app-missing {
    justify-content: center;
    align-items: center;
    text-align: center;
    background: none;
    border-style: dashed;
  }

  .app-missing .app-name {
    color: var(--text-muted);
  }

  .value {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-primary);
  }

  /* An app tile is narrower than a panel, so its labels take their own width. */
  .app :global(.kv) {
    grid-template-columns: max-content minmax(0, 1fr);
  }

  .app-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
  }

  .app-name {
    margin: 0;
    color: var(--text-primary);
    text-transform: capitalize;
  }

  .app-label {
    text-transform: capitalize;
  }

  .app-problem {
    margin: 0;
    overflow-wrap: anywhere;
  }
</style>
