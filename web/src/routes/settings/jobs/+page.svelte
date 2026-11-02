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
  import {
    JobForm,
    NetworkAccessCard,
    SettingsLayout,
    type JobSaved,
  } from '$lib/components/settings';
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
  const SYNC_READ_MS = 5_000;
  const SYNC_WAIT_MS = 120_000;

  const identity = getCurrentUser();

  let data = $state<ScheduledJobs | null>(null);
  let loadError = $state('');
  let loading = $state(true);
  let busy = $state(false);
  let selectedId = $state<number | 'new' | null>(null);
  let syncIsSlow = $state(false);
  const deleting = createConfirm<ScheduledJob>();
  const loads = latest();
  const syncWaits = latest();
  let syncTimer: ReturnType<typeof setTimeout> | undefined;
  onDestroy(() => {
    loads.dispose();
    syncWaits.dispose();
    clearTimeout(syncTimer);
  });

  // A pending row has a null id too, so nothing selected must not match it.
  const selected = $derived(
    selectedId === null ? null : (data?.jobs.find((job) => job.id === selectedId) ?? null),
  );
  const creating = $derived(selectedId === 'new' && Boolean(data?.file.readable));
  const anyPending = $derived(data?.jobs.some((job) => job.pending) ?? false);
  const file = $derived(data?.file ?? { readable: true, errors: [] });
  // With no workspace there is no file to fix and nothing to list.
  const fileAlert = $derived(
    file.errors.length > 0 || (!file.readable && (data?.jobs.length ?? 0) > 0),
  );

  const selection = createUrlSelection<{ job: number | 'new' | null }>({
    key: 'settingsJob',
    params: ['job'],
    encode: ({ job }): Record<string, string> => (job === null ? {} : { job: String(job) }),
    decode: (params) => {
      const text = params.job ?? '';
      if (text === 'new') return { job: 'new' };
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
    if (!data || selectedId === null || selected || creating) return;
    const wasNew = selectedId === 'new';
    selection.replace({ job: null });
    selectedId = null;
    if (!wasNew) notifyWarning(JOB_GONE);
  }

  /** `quiet` is a read nobody asked for, which says nothing when it fails. */
  async function refresh({ quiet = false } = {}) {
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
      else if (quiet) return;
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

  /** A save leaves the row to the daemon's next sync, about a minute away, so read until
   *  no row is pending, each read awaited before the next is timed. */
  async function waitForSync() {
    const current = syncWaits.next();
    clearTimeout(syncTimer);
    syncIsSlow = false;
    const deadline = Date.now() + SYNC_WAIT_MS;
    await refresh();
    while (current() && anyPending) {
      if (Date.now() >= deadline) {
        syncIsSlow = true;
        return;
      }
      await new Promise((resolve) => {
        syncTimer = setTimeout(resolve, SYNC_READ_MS);
      });
      if (current()) await refresh({ quiet: true });
    }
  }

  async function saved({ accessLost }: JobSaved) {
    selection.push({ job: null });
    if (accessLost)
      notifyWarning('The job is saved, and it will ask for approval again before its next run.');
    await waitForSync();
  }

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

  // A one-time job waiting for an answer would run limited and delete itself with the request.
  function isHolding(job: ScheduledJob): boolean {
    return job.network?.job?.waiting ?? false;
  }

  function rowActions(job: ScheduledJob): KebabItem[] {
    const items: KebabItem[] = [
      { label: job.editable ? 'Edit' : 'View', onSelect: () => selection.push({ job: job.id }) },
    ];
    if (isModuleJob(job)) return items;
    if (!isHolding(job)) items.push({ label: 'Run now', disabled: busy, onSelect: () => run(job) });
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

{#snippet newJob()}
  {#if data?.file.readable && data.jobs.length > 0}
    <Button size="sm" variant="secondary" onclick={() => selection.push({ job: 'new' })}
      >New job</Button
    >
  {/if}
{/snippet}

<SettingsLayout
  description={selected || creating
    ? undefined
    : 'Your scheduled jobs and what each may reach. Work you start yourself is not limited here.'}
  headerActions={selected || creating ? undefined : newJob}
  viewKey={selectedId}
  {loading}
  {loadError}
  onRetry={retry}
>
  {#if data && creating}
    <PageHeader title="New job" back={{ href: LIST_HREF, label: 'Scheduled jobs' }} align="start" />
    <JobForm
      job={null}
      options={data.options}
      timezone={data.timezone}
      onSaved={saved}
      onReload={refresh}
    />
  {:else if data && selected}
    {@const job = selected}
    <PageHeader title={job.name} back={{ href: LIST_HREF, label: 'Scheduled jobs' }} align="start">
      {#snippet badge()}
        <Badge size="sm" variant={STATE_VARIANT[job.state]}>{STATE_LABEL[job.state]}</Badge>
        {#if job.once}<Badge size="sm" variant="neutral">One time</Badge>{/if}
      {/snippet}
      {#snippet actions()}
        {#if !isModuleJob(job)}
          <div class="hstack wrap">
            {#if !isHolding(job)}
              <Button size="sm" variant="secondary" disabled={busy} onclick={() => run(job)}
                >Run now</Button
              >
            {/if}
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

    {#key job.id}
      <JobForm
        {job}
        options={data.options}
        timezone={data.timezone}
        onSaved={saved}
        onReload={refresh}
      />
    {/key}

    {#if job.network}
      <NetworkAccessCard
        title="Network access"
        subject={job.network}
        showJobText={false}
        nextRunAt={job.next_run_at}
        externalSkills={data.external_skills}
        onChanged={refresh}
      />
    {/if}
  {:else if data}
    {#if fileAlert}
      <Alert title="CRON.md has a problem">
        <p>
          {#if !file.readable}
            Jobs are read only until it is fixed, because saving would drop any job that did not
            load.
          {/if}
          <a href={CONFIG_EDITOR_HREF}>Open it under Files, Configuration.</a>
        </p>
        {#if file.errors.length > 0}
          <ul>
            {#each file.errors as problem, index (index)}<li>{problem}</li>{/each}
          </ul>
        {/if}
      </Alert>
    {/if}

    {#if syncIsSlow && anyPending}
      <p class="caption">This is taking longer than usual.</p>
    {/if}

    {#if data.jobs.length === 0}
      <EmptyState
        actionLabel={data.file.readable ? 'New job' : undefined}
        onaction={() => selection.push({ job: 'new' })}
        >You have no scheduled jobs yet. Ask for one in chat, or add one here.</EmptyState
      >
    {:else}
      <Card>
        <div class="table-scroll table--cards">
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
                  <td class="col-job cell-primary">
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
                  <td class="col-schedule" data-label="Schedule"><code>{job.cron}</code></td>
                  <td class="col-state" data-label="State">
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
                  <td class="col-access" class:card-hidden={!job.network} data-label="Access">
                    {#if job.network}
                      {@const access = accessBadge(job.network)}
                      <Badge size="sm" variant={access.variant}>{access.label}</Badge>
                    {/if}
                  </td>
                  <td
                    class="col-last truncate"
                    class:card-hidden={job.id === null}
                    data-label="Last run"
                  >
                    {#if job.id !== null}
                      <Timestamp value={job.last_run_at} format="relative" empty="Never" />
                    {/if}
                  </td>
                  <td
                    class="num col-failures"
                    class:failing={job.consecutive_failures > 0}
                    class:card-hidden={job.consecutive_failures === 0}
                    data-label="Failures"
                  >
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
  @container (max-width: 40rem) {
    .jobs-grid {
      min-width: 0;
    }
    .jobs-grid td {
      width: auto;
    }
    .jobs-grid td.num {
      text-align: left;
    }
  }
</style>
