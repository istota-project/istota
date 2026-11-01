<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    AuthError,
    deleteScheduledJob,
    getScheduledJobs,
    runScheduledJob,
    setScheduledJobEnabled,
    type ScheduledJob,
    type ScheduledJobs,
  } from '$lib/api';
  import { NetworkAccessCard, SettingsLayout } from '$lib/components/settings';
  import { createUrlSelection } from '$lib/navigation/urlSelection.svelte';
  import { accessBadge, STATE_LABEL, STATE_VARIANT } from '$lib/settings/jobState';
  import { getCurrentUser } from '$lib/userContext';
  import {
    Alert,
    Badge,
    Button,
    Card,
    ConfirmDialog,
    Disclosure,
    EmptyState,
    KebabMenu,
    PageHeader,
    Timestamp,
    type KebabItem,
  } from '@minot/ui';
  import { notifyError, notifySuccess, notifyWarning } from '@minot/ui/stores/notices';
  import { createConfirm } from '@minot/ui/ui/confirm.svelte';
  import { latest } from '@minot/ui/ui/latest';

  const LIST_HREF = `${base}/settings/jobs`;
  const CONFIG_EDITOR_HREF = `${base}/files?view=config`;
  const JOB_GONE = 'This job no longer exists.';
  const FALLBACK_LOAD_ERROR = 'Scheduled jobs could not be loaded.';
  const MODULE_PREFIX = '_module.';

  const identity = getCurrentUser();

  let data = $state<ScheduledJobs | null>(null);
  let loadError = $state('');
  let loading = $state(true);
  let busy = $state(false);
  let selectedId = $state<number | null>(null);
  const deleting = createConfirm<ScheduledJob>();
  const loads = latest();
  onDestroy(() => loads.dispose());

  // A pending row has a null id too, so nothing selected must not match it.
  const selected = $derived(
    selectedId === null ? null : (data?.jobs.find((job) => job.id === selectedId) ?? null),
  );
  const file = $derived(data?.file ?? { readable: true, errors: [] });
  // With no workspace there is no file to fix and nothing to list.
  const fileAlert = $derived(
    file.errors.length > 0 || (!file.readable && (data?.jobs.length ?? 0) > 0),
  );

  const selection = createUrlSelection<{ job: number | null }>({
    key: 'settingsJob',
    params: ['job'],
    encode: ({ job }): Record<string, string> => (job === null ? {} : { job: String(job) }),
    decode: (params) => {
      const text = params.job ?? '';
      return { job: /^\d+$/.test(text) ? Number(text) : null };
    },
    read: () => (data ? { job: selectedId } : null),
    apply: ({ job }) => {
      selectedId = job;
      leaveIfGone();
    },
  });
  selection.start();

  /** A one-time job deletes itself once it fires, so the job on screen can go. */
  function leaveIfGone() {
    if (!data || selectedId === null || selected) return;
    selection.replace({ job: null });
    selectedId = null;
    notifyWarning(JOB_GONE);
  }

  async function refresh() {
    const current = loads.next();
    try {
      const result = await getScheduledJobs();
      if (!current()) return;
      const first = data === null;
      data = result;
      loadError = '';
      if (first) selectedId = selection.current()?.job ?? null;
      leaveIfGone();
    } catch (e) {
      if (!current()) return;
      if (e instanceof AuthError) identity.expireSession();
      else if (data) notifyError((e as Error).message || FALLBACK_LOAD_ERROR);
      else loadError = (e as Error).message || FALLBACK_LOAD_ERROR;
    } finally {
      if (current()) loading = false;
    }
  }

  function retry() {
    loading = true;
    loadError = '';
    void refresh();
  }

  onMount(refresh);

  function statusOf(e: unknown): number | undefined {
    return (e as { status?: number }).status;
  }

  async function act(work: () => Promise<unknown>) {
    if (busy) return;
    busy = true;
    try {
      await work();
    } catch (e) {
      if (e instanceof AuthError) identity.expireSession();
      // On the job's own page the reload below says so, as it leaves.
      else if (statusOf(e) === 404) {
        if (selectedId === null) notifyWarning(JOB_GONE);
      } else notifyError((e as Error).message || 'That did not work.');
    } finally {
      await refresh();
      busy = false;
    }
  }

  function setEnabled(job: ScheduledJob, enabled: boolean) {
    if (job.id === null) return;
    const id = job.id;
    void act(async () => {
      const { outcome } = await setScheduledJobEnabled(id, enabled);
      if (outcome === 'db_only')
        notifyWarning(
          `CRON.md could not be updated, so the next sync will set ${job.name} back to what the file says.`,
        );
    });
  }

  function run(job: ScheduledJob) {
    if (job.id === null) return;
    const id = job.id;
    void act(async () => {
      await runScheduledJob(id);
      notifySuccess(`${job.name} started.`);
    });
  }

  async function confirmDelete() {
    const job = deleting.target;
    if (!job || job.id === null) return;
    try {
      await deleteScheduledJob(job.id);
    } catch (e) {
      if (e instanceof AuthError) {
        identity.expireSession();
        return;
      }
      if (statusOf(e) !== 404) throw e;
    }
    if (selectedId === job.id) {
      selection.replace({ job: null });
      selectedId = null;
    }
    notifySuccess(`${job.name} deleted.`);
    await refresh();
  }

  function isModuleJob(job: ScheduledJob): boolean {
    return job.name.startsWith(MODULE_PREFIX);
  }

  function rowActions(job: ScheduledJob): KebabItem[] {
    const items: KebabItem[] = [{ label: 'View', onSelect: () => selection.push({ job: job.id }) }];
    if (isModuleJob(job)) return items;
    items.push({ label: 'Run now', disabled: busy, onSelect: () => run(job) });
    items.push(
      job.state === 'running'
        ? { label: 'Disable', disabled: busy, onSelect: () => setEnabled(job, false) }
        : { label: 'Enable', disabled: busy, onSelect: () => setEnabled(job, true) },
    );
    // A row the file does not hold goes at the next sync; nothing here can remove it.
    if (job.editable)
      items.push({
        label: 'Delete',
        danger: true,
        disabled: busy,
        onSelect: () => deleting.ask(job),
      });
    return items;
  }
</script>

{#snippet cronFix()}
  <a href={CONFIG_EDITOR_HREF}>Fix it in CRON.md, under Files, Configuration.</a>
{/snippet}

<SettingsLayout
  description={selected
    ? undefined
    : 'Your scheduled jobs and what each may reach. Work you start yourself is not limited here.'}
  {loading}
  {loadError}
  onRetry={retry}
>
  {#if data && selected}
    {@const job = selected}
    {@const definition = job.definition}
    <PageHeader title={job.name} back={{ href: LIST_HREF, label: 'Scheduled jobs' }} align="start">
      {#snippet badge()}
        <Badge size="sm" variant={STATE_VARIANT[job.state]}>{STATE_LABEL[job.state]}</Badge>
        {#if job.once}<Badge size="sm" variant="neutral">One time</Badge>{/if}
      {/snippet}
      {#snippet actions()}
        {#if !isModuleJob(job)}
          <div class="hstack wrap">
            <Button size="sm" variant="secondary" disabled={busy} onclick={() => run(job)}
              >Run now</Button
            >
            {#if job.state === 'running'}
              <Button
                size="sm"
                variant="secondary"
                disabled={busy}
                onclick={() => setEnabled(job, false)}>Disable</Button
              >
            {:else}
              <Button
                size="sm"
                variant="secondary"
                disabled={busy}
                onclick={() => setEnabled(job, true)}>Enable</Button
              >
            {/if}
            {#if job.editable}
              <Button size="sm" variant="danger" disabled={busy} onclick={() => deleting.ask(job)}
                >Delete</Button
              >
            {/if}
          </div>
        {/if}
      {/snippet}
    </PageHeader>

    {#if job.state === 'disabled'}
      <p class="caption">
        This job is disabled, so it does not run. What is approved below applies if it is enabled
        again.
      </p>
    {:else if job.state === 'suspended'}
      <p class="caption">
        Istota stopped running this job after repeated failures, which a refused host can cause.
        Enable it to start it again.
      </p>
    {/if}

    <Card title="Summary">
      <dl class="kv">
        <dt>Schedule</dt>
        <dd><code>{job.cron}</code></dd>
        <dt>Last run</dt>
        <dd><Timestamp value={job.last_run_at} format="datetime" empty="Never" /></dd>
        <dt>Last success</dt>
        <dd><Timestamp value={job.last_success_at} format="datetime" empty="Never" /></dd>
        <dt>Failures in a row</dt>
        <dd class="num" class:failing={job.consecutive_failures > 0}>
          {job.consecutive_failures}
        </dd>
      </dl>
      {#if job.last_error}
        <Disclosure summary="Last error">
          <pre class="job-text failing">{job.last_error}</pre>
        </Disclosure>
      {/if}
    </Card>

    <Card title="Definition" description="Read only here. To change this job, edit CRON.md.">
      <dl class="kv">
        <dt>Kind</dt>
        <dd>{definition.kind === 'command' ? 'Command' : 'Prompt'}</dd>
        {#if definition.prompt_file}
          <dt>Prompt file</dt>
          <dd class="mono">{definition.prompt_file}</dd>
        {/if}
        {#if definition.target}
          <dt>Delivers to</dt>
          <dd class="mono">{definition.target}</dd>
        {/if}
        {#if definition.model}
          <dt>Model</dt>
          <dd class="mono">{definition.model}</dd>
        {/if}
        {#if definition.effort}
          <dt>Effort</dt>
          <dd>{definition.effort}</dd>
        {/if}
        {#if definition.brain}
          <dt>Brain</dt>
          <dd class="mono">{definition.brain}</dd>
        {/if}
        {#if definition.provider}
          <dt>Provider</dt>
          <dd class="mono">{definition.provider}</dd>
        {/if}
        {#if definition.silent_unless_action}
          <dt>Quiet</dt>
          <dd>Stays quiet unless there is something to act on</dd>
        {/if}
        {#if definition.skip_log_channel}
          <dt>Log channel</dt>
          <dd>Left out of the log channel</dd>
        {/if}
        {#if definition.publish_shared_kv}
          <dt>Publishes to</dt>
          <dd>
            <code>{definition.publish_shared_kv}</code>{definition.publish_shared_kv_trusted
              ? ', marked trusted'
              : ''}
          </dd>
        {/if}
        {#if definition.egress.length > 0}
          <dt>Asks for</dt>
          <dd class="mono">{definition.egress.join(', ')}</dd>
        {/if}
      </dl>
      {#if definition.prompt}<pre class="job-text">{definition.prompt}</pre>{/if}
      {#if definition.command}<pre class="job-text">{definition.command}</pre>{/if}
      <p class="caption">
        <a href={CONFIG_EDITOR_HREF}>Open CRON.md under Files, Configuration.</a>
      </p>
    </Card>

    {#if job.network}
      <NetworkAccessCard
        title="Network access"
        subject={job.network}
        showJobText={false}
        externalSkills={data.external_skills}
        onChanged={refresh}
      />
    {/if}
  {:else if data}
    {#if fileAlert}
      <Alert title="CRON.md has a problem">
        <p>
          Jobs are read only until it is fixed, because saving would drop any job that did not load.
          <a href={CONFIG_EDITOR_HREF}>Open it under Files, Configuration.</a>
        </p>
        {#if file.errors.length > 0}
          <ul>
            {#each file.errors as problem, index (index)}<li>{problem}</li>{/each}
          </ul>
        {/if}
      </Alert>
    {/if}

    {#if data.jobs.length === 0}
      <EmptyState
        >You have no scheduled jobs yet. Ask for one in chat, or add it to CRON.md.</EmptyState
      >
    {:else}
      <Card>
        <div class="table-scroll">
          <table class="table table--fixed jobs-grid" aria-label="Scheduled jobs">
            <thead>
              <tr>
                <th scope="col" class="col-job">Job</th>
                <th scope="col" class="col-schedule">Schedule</th>
                <th scope="col" class="col-state">State</th>
                <th scope="col" class="col-access">Access</th>
                <th scope="col" class="col-last">Last run</th>
                <th scope="col" class="num col-failures">Failures</th>
                <th scope="col" class="col-actions" aria-label="Actions"></th>
              </tr>
            </thead>
            <tbody>
              {#each data.jobs as job (job.name)}
                <tr>
                  <td class="col-job">
                    <div class="hstack wrap job-name">
                      {#if job.id !== null}
                        <a class="link-quiet" href="{LIST_HREF}?job={job.id}">{job.name}</a>
                      {:else}
                        <span>{job.name}</span>
                      {/if}
                      {#if job.once}<Badge size="sm" variant="neutral">One time</Badge>{/if}
                    </div>
                    {#if job.file_error}
                      <p class="caption">{job.file_error} {@render cronFix()}</p>
                    {/if}
                  </td>
                  <td class="col-schedule"><code>{job.cron}</code></td>
                  <td class="col-state">
                    {#if job.pending}
                      <span class="caption">Waiting for the next sync</span>
                    {:else if job.file_error}
                      <Badge size="sm" variant="neutral">Not running</Badge>
                    {:else}
                      <Badge size="sm" variant={STATE_VARIANT[job.state]}
                        >{STATE_LABEL[job.state]}</Badge
                      >
                    {/if}
                  </td>
                  <td class="col-access">
                    {#if job.network}
                      {@const access = accessBadge(job.network)}
                      <Badge size="sm" variant={access.variant}>{access.label}</Badge>
                    {/if}
                  </td>
                  <td class="col-last truncate">
                    {#if job.id !== null}
                      <Timestamp value={job.last_run_at} format="relative" empty="Never" />
                    {/if}
                  </td>
                  <td class="num col-failures" class:failing={job.consecutive_failures > 0}>
                    {job.consecutive_failures}
                  </td>
                  <td class="col-actions">
                    {#if job.id !== null}
                      <KebabMenu items={rowActions(job)} ariaLabel="Actions for {job.name}" />
                    {/if}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </Card>
    {/if}
  {/if}
</SettingsLayout>

<ConfirmDialog
  bind:open={deleting.open}
  title="Delete job"
  message={deleting.target
    ? `Delete ${deleting.target.name}? It is removed from CRON.md and stops running. This cannot be undone.`
    : ''}
  confirmLabel="Delete"
  busyLabel="Deleting…"
  confirmVariant="danger"
  onConfirm={confirmDelete}
/>

<style>
  .job-name {
    overflow-wrap: anywhere;
  }
  .job-text {
    margin: 0;
    max-height: 16rem;
    overflow: auto;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-size: var(--text-sm);
  }
  .failing {
    color: var(--status-danger-fg);
  }
  /* Job is the unsized column: the min-width reserves it 12rem past the sized ones. */
  .jobs-grid {
    min-width: 49rem;
  }
  .col-schedule {
    width: 8rem;
  }
  .col-state {
    width: 7.5rem;
  }
  .col-access {
    width: 8rem;
  }
  .col-last {
    width: 6.5rem;
  }
  .col-failures {
    width: 5rem;
  }
</style>
