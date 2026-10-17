<script lang="ts">
  import { base } from '$app/paths';
  import {
    getAdminConfig,
    getAdminUsers,
    type AdminConfigView,
    type AdminServarrApp,
    type AdminServarrConnection,
    type ConfigChangeSet,
  } from '$lib/api';
  import RequestProgress from '$lib/admin/config/RequestProgress.svelte';
  import ReviewDialog from '$lib/admin/config/ReviewDialog.svelte';
  import { buildChangeSet, changeCount } from '$lib/admin/config/changeSet';
  import { Badge, Button, NoticeBanner } from '$lib/components/ui';
  import { formatRelative } from '$lib/dateFormat';
  import { waitForDoctorRun } from '$lib/doctorRun';
  import { notifyError } from '$lib/stores/notices';
  import { Clapperboard } from '@lucide/svelte';
  import CheckResult from './CheckResult.svelte';
  import ConnectionCard from './ConnectionCard.svelte';
  import ServarrEditForm from './ServarrEditForm.svelte';
  import {
    formProblems,
    rebaseForm,
    servarrDraft,
    servarrForm,
    servarrReadOnlyReason,
    viewPrivateHosts,
    type ServarrForm,
  } from './servarrForm';

  interface Props {
    connection: AdminServarrConnection;
    /** Re-read `/admin/connections`: after a check run, and after a change. */
    onchecked: () => Promise<void> | void;
  }

  let { connection, onchecked }: Props = $props();

  let checking = $state(false);
  let unanswered = $state(false);

  // Editing goes through the config editor's own change set, review, step-up
  // and request flow; the card adds no endpoint of its own.
  let opening = $state(false);
  let configView = $state<AdminConfigView | null>(null);
  let readOnly = $state<string | null>(null);
  let initial = $state<ServarrForm | null>(null);
  let form = $state<ServarrForm | null>(null);
  let users = $state<string[]>([]);
  let problems = $state<Record<string, string>>({});
  let changeSet = $state<ConfigChangeSet | null>(null);
  let requestId = $state<number | null>(null);
  let notice = $state('');

  let draft = $derived(form && initial ? servarrDraft(form, initial) : null);
  let pendingCount = $derived(draft ? changeCount(draft) : 0);

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

  function closeEditor() {
    form = null;
    initial = null;
    problems = {};
    changeSet = null;
  }

  /** The editable state of config.toml, or a reason it cannot be changed here. */
  async function loadConfig(): Promise<AdminConfigView> {
    const view = await getAdminConfig();
    configView = view;
    readOnly = servarrReadOnlyReason(view);
    return view;
  }

  async function openEditor() {
    if (opening) return;
    opening = true;
    notice = '';
    try {
      const [view, listed] = await Promise.all([loadConfig(), getAdminUsers().catch(() => null)]);
      users = listed ? listed.users.map((user) => user.user_id) : [];
      if (readOnly) return;
      const start = servarrForm(connection, viewPrivateHosts(view), view.secrets);
      initial = start;
      form = structuredClone(start);
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'The configuration could not be loaded');
    } finally {
      opening = false;
    }
  }

  function review() {
    if (!form || !draft || !configView?.document_digest) return;
    problems = formProblems(form);
    if (Object.keys(problems).length) return;
    changeSet = buildChangeSet(draft, configView.document_digest);
  }

  /** config.toml changed under the card: reload, keep what the admin changed. */
  async function onConflict() {
    changeSet = null;
    const edited = form ? $state.snapshot(form) : null;
    const before = initial ? $state.snapshot(initial) : null;
    let view: AdminConfigView;
    try {
      view = await loadConfig();
      if (readOnly || !edited || !before) closeEditor();
      await onchecked();
    } catch (e) {
      notifyError(e instanceof Error ? e.message : 'The configuration could not be reloaded');
      return;
    }
    if (!form || !edited || !before) return;
    const after = servarrForm(connection, viewPrivateHosts(view), view.secrets);
    initial = after;
    form = rebaseForm(edited, before, after);
    notice =
      'config.toml changed on the host since this form was opened. The form now shows the new ' +
      'values, and your changes are kept. Review them again.';
  }

  function onSubmitted(id: number) {
    closeEditor();
    notice = '';
    requestId = id;
  }

  function onRequestOpen(id: number) {
    closeEditor();
    notice = 'Another change is already waiting for the host. It is shown below.';
    requestId = id;
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
    <p class="summary wide">
      This install has no arr apps for the servarr skill: no <code>[servarr.apps]</code> table is set.
    </p>
  {:else if form}
    <!-- The editor takes the read view's place rather than stacking under a
         copy of what it edits. -->
    <div class="wide">
      <ServarrEditForm bind:form {problems} {users} />
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
            <h3 class="app-name">{app.app}</h3>
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
          <h3 class="app-name">{app}</h3>
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

  {#if readOnly}
    <div class="wide">
      <NoticeBanner title="Not changed from here" variant="info" collapsed={false}>
        <p>{readOnly}</p>
      </NoticeBanner>
    </div>
  {/if}

  {#if notice}
    <p class="banner warn wide" role="status">{notice}</p>
  {/if}

  {#if requestId !== null}
    <div class="wide">
      {#key requestId}
        <RequestProgress
          {requestId}
          onApplied={() => void onchecked()}
          onDismiss={() => {
            requestId = null;
            notice = '';
            void onchecked();
          }}
        />
      {/key}
    </div>
  {/if}

  {#snippet meta()}
    {#if form}
      <p class="caption">
        Changes go through the same review, confirmation and host applier as the Configuration page,
        and Istota restarts to load them. A new key is sealed until the host writes it.
      </p>
    {:else if connection.status !== 'disabled'}
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
    {#if form}
      <Button
        variant="secondary"
        onclick={() => {
          closeEditor();
          notice = '';
        }}>Cancel</Button
      >
      <Button variant="primary" onclick={review} disabled={pendingCount === 0}
        >Review changes</Button
      >
    {:else}
      <Button variant="secondary" href={`${base}/admin/config?section=servarr`}>Configure</Button>
      {#if requestId === null}
        <Button variant="secondary" onclick={openEditor} loading={opening} loadingLabel="Loading…"
          >Edit</Button
        >
      {/if}
      {#if connection.apps.length > 0}
        <Button variant="secondary" onclick={checkNow} loading={checking} loadingLabel="Checking…"
          >Check now</Button
        >
      {/if}
    {/if}
  {/snippet}
</ConnectionCard>

{#if changeSet}
  <ReviewDialog
    {changeSet}
    onClose={() => (changeSet = null)}
    {onSubmitted}
    {onConflict}
    {onRequestOpen}
  />
{/if}

<style>
  .summary {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }

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
    font-size: var(--text-sm);
    font-weight: 600;
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

  code {
    overflow-wrap: anywhere;
    user-select: text;
  }
</style>
