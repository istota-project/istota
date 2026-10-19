<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    AuthError,
    GroupApiError,
    deleteAdminScheduledJob,
    getAdminScheduledJobs,
    setAdminScheduledJobEnabled,
    type AdminStatsJob,
    type StepUpProof,
  } from '$lib/api';
  import {
    Badge,
    ConfirmDialog,
    EmptyState,
    Input,
    KebabMenu,
    PageLoading,
    type KebabItem,
  } from '$lib/components/ui';
  import { SettingsCard, StepUpDialog } from '$lib/components/settings';
  import { formatRelative } from '$lib/dateFormat';
  import { notifyError, notifySuccess, notifyWarning } from '$lib/stores/notices';

  type JobState = 'running' | 'disabled' | 'suspended';

  let jobs = $state<AdminStatsJob[] | null>(null);
  let loadError = $state('');
  let filter = $state('');
  let busy = $state(false);
  let disabling = $state<AdminStatsJob | null>(null);
  let deleting = $state<AdminStatsJob | null>(null);
  let deleteStage = $state<'confirm' | 'step-up' | null>(null);

  function isModuleJob(job: AdminStatsJob): boolean {
    return job.name.startsWith('_module.');
  }

  /** Two columns, two authors: `enabled` is the user's CRON.md, a suspension
   *  is the scheduler giving up after repeated failures. A job that is both
   *  reads as disabled, the state someone chose. */
  function stateOf(job: AdminStatsJob): JobState {
    if (!job.enabled) return 'disabled';
    return job.auto_disabled_at ? 'suspended' : 'running';
  }

  const STATE_LABEL = { running: 'Running', disabled: 'Disabled', suspended: 'Suspended' } as const;
  const STATE_VARIANT = { running: 'success', disabled: 'neutral', suspended: 'warn' } as const;

  const needle = $derived(filter.trim().toLowerCase());
  const visible = $derived(
    (jobs ?? []).filter(
      (job) =>
        !needle ||
        job.user_id.toLowerCase().includes(needle) ||
        job.name.toLowerCase().includes(needle),
    ),
  );
  const userJobs = $derived(visible.filter((job) => !isModuleJob(job)));
  const moduleJobs = $derived(visible.filter(isModuleJob));
  const runningCount = $derived((jobs ?? []).filter((job) => stateOf(job) === 'running').length);

  async function load() {
    try {
      jobs = (await getAdminScheduledJobs()).jobs;
      loadError = '';
    } catch (e) {
      if (e instanceof AuthError) {
        window.location.assign(`${base}/login`);
        return;
      }
      const message = e instanceof Error ? e.message : 'Scheduled jobs could not be loaded.';
      // A failed first load has nowhere else to go; a failed reload keeps the list.
      if (jobs === null) loadError = message;
      else notifyError(message);
    }
  }
  onMount(load);

  function failed(e: unknown, fallback: string) {
    if (e instanceof AuthError) {
      window.location.assign(`${base}/login`);
      return;
    }
    notifyError(e instanceof Error && e.message ? e.message : fallback);
  }

  async function setEnabled(job: AdminStatsJob, enabled: boolean) {
    disabling = null;
    if (busy) return;
    busy = true;
    try {
      const { outcome } = await setAdminScheduledJobEnabled(job.id, enabled);
      if (outcome === 'db_only')
        notifyWarning(
          `${job.user_id}'s CRON.md could not be updated, so the next sync will set ` +
            `${job.name} back to what the file says.`,
        );
    } catch (e) {
      failed(e, `The job could not be ${enabled ? 'enabled' : 'disabled'}.`);
    } finally {
      await load();
      busy = false;
    }
  }

  function startDelete(job: AdminStatsJob) {
    deleting = job;
    deleteStage = 'confirm';
  }

  function stopDelete() {
    deleting = null;
    deleteStage = null;
  }

  async function submitDelete(proof: StepUpProof) {
    const job = deleting;
    if (!job) return;
    try {
      await deleteAdminScheduledJob(job.id, job.name, proof);
      notifySuccess(`${job.name} deleted.`);
    } catch (e) {
      // A refused code or password stays in the step-up dialog, which reads `reason`.
      if (e instanceof GroupApiError && e.status === 403 && e.reason) throw e;
      failed(e, 'The job could not be deleted.');
    }
    stopDelete();
    await load();
  }

  function rowActions(job: AdminStatsJob): KebabItem[] {
    const items: KebabItem[] = [
      stateOf(job) === 'running'
        ? { label: 'Disable', disabled: busy, onSelect: () => (disabling = job) }
        : { label: 'Enable', disabled: busy, onSelect: () => setEnabled(job, true) },
    ];
    if (!isModuleJob(job))
      items.push({
        label: 'Delete',
        danger: true,
        disabled: busy,
        onSelect: () => startDelete(job),
      });
    return items;
  }
</script>

<div class="settings admin-page card-pane">
  {#if jobs === null && !loadError}
    <PageLoading label="Loading scheduled jobs…" />
  {:else if jobs === null}
    <div class="center-msg error">{loadError}</div>
  {:else}
    <div class="filter-row">
      <Input
        type="search"
        aria-label="Filter scheduled jobs"
        placeholder="Filter by user or job"
        bind:value={filter}
      />
      <span class="caption">{runningCount} running · {jobs.length - runningCount} stopped</span>
    </div>

    {#snippet table(rows: AdminStatsJob[], label: string)}
      <div class="table-scroll">
        <table class="grid jobs-grid" aria-label={label}>
          <thead
            ><tr
              ><th scope="col" class="col-user">User</th>
              <th scope="col" class="col-job">Job</th>
              <th scope="col" class="col-schedule">Schedule</th>
              <th scope="col" class="col-state">State</th>
              <th scope="col" class="col-last">Last run</th>
              <th scope="col" class="num col-failures">Failures</th>
              <th scope="col" class="col-actions" aria-label="Actions"></th></tr
            ></thead
          >
          <tbody>
            {#each rows as job (job.id)}
              {@const state = stateOf(job)}
              <tr>
                <td class="col-user">{job.user_id}</td>
                <td class="col-job">
                  <div class="cell-main">{job.name}</div>
                  {#if job.last_error}
                    <details class="last-error">
                      <summary class="caption">Last error</summary>
                      <pre>{job.last_error}</pre>
                    </details>
                  {/if}
                </td>
                <td class="col-schedule"><code>{job.cron}</code></td>
                <td class="col-state">
                  <Badge size="sm" variant={STATE_VARIANT[state]}>{STATE_LABEL[state]}</Badge>
                </td>
                <td class="col-last truncate"
                  >{job.last_run_at ? formatRelative(job.last_run_at) : 'Never'}</td
                >
                <td class="num col-failures" class:failing={job.consecutive_failures > 0}
                  >{job.consecutive_failures}</td
                >
                <td class="col-actions">
                  <KebabMenu
                    items={rowActions(job)}
                    ariaLabel="Actions for {job.user_id}/{job.name}"
                  />
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/snippet}

    <SettingsCard
      title="User jobs"
      description="Jobs from each user's CRON.md. Disabling writes the change to that file, so the user can turn the job back on with !cron enable. Deleting removes it from the file."
    >
      {#if userJobs.length}
        {@render table(userJobs, 'User jobs')}
      {:else if needle}
        <EmptyState size="sm">No user jobs match the filter.</EmptyState>
      {:else}
        <EmptyState size="sm">No user jobs yet.</EmptyState>
      {/if}
    </SettingsCard>

    <SettingsCard
      title="Module pollers"
      description="Jobs a module keeps for each user who has it on. They can be disabled but not deleted, since the module would create them again."
    >
      {#if moduleJobs.length}
        {@render table(moduleJobs, 'Module pollers')}
      {:else if needle}
        <EmptyState size="sm">No module pollers match the filter.</EmptyState>
      {:else}
        <EmptyState size="sm">No module pollers.</EmptyState>
      {/if}
    </SettingsCard>
  {/if}
</div>

<ConfirmDialog
  open={disabling !== null}
  title="Disable job"
  message={disabling
    ? `Disable ${disabling.name} for ${disabling.user_id}? It stops running until it is enabled again.`
    : ''}
  confirmLabel="Disable"
  confirmVariant="primary"
  onConfirm={() => {
    if (disabling) void setEnabled(disabling, false);
  }}
  onCancel={() => (disabling = null)}
/>

{#if deleting}
  <ConfirmDialog
    open={deleteStage === 'confirm'}
    title="Delete job"
    message={`Delete ${deleting.name} for ${deleting.user_id}? It is removed from their CRON.md and stops running. This cannot be undone.`}
    confirmLabel="Delete"
    confirmVariant="danger"
    challenge={deleting.name}
    onConfirm={() => (deleteStage = 'step-up')}
    onCancel={stopDelete}
  />
{/if}

{#if deleteStage === 'step-up'}
  <StepUpDialog action="admin_cron_delete" onConfirm={submitDelete} onCancel={stopDelete} />
{/if}

<style>
  .filter-row {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    margin-bottom: var(--space-4);
    max-width: 36rem;
  }
  .filter-row :global(.ui-input) {
    flex: 1;
  }
  .cell-main {
    overflow-wrap: anywhere;
  }
  .last-error summary {
    cursor: pointer;
  }
  .last-error pre {
    margin: var(--space-1) 0 0;
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .num {
    text-align: right;
  }
  .failing {
    color: var(--status-danger-fg);
  }
  /* Job is the unsized column: the min-width reserves it 12rem past the sized
     ones, and adminTables.test.ts holds the sum. */
  .jobs-grid {
    min-width: 49rem;
  }
  .col-user {
    width: 8rem;
  }
  .col-schedule {
    width: 8rem;
  }
  .col-state {
    width: 7rem;
  }
  .col-last {
    width: 6.5rem;
  }
  .col-failures {
    width: 5rem;
  }
  .jobs-grid .col-actions {
    width: 2.5rem;
    text-align: right;
  }
  /* Narrow: keep every column and scroll sideways; the min-width tightens
     with User and Schedule. */
  @container settings (max-width: 40rem) {
    .jobs-grid {
      min-width: 46.5rem;
    }
    .col-user {
      width: 6.5rem;
    }
    .col-schedule {
      width: 7rem;
    }
  }
</style>
