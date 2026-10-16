<script lang="ts">
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    AuthError,
    GroupApiError,
    getAdminBriefings,
    holdAdminBriefing,
    resumeAdminBriefing,
    deleteAdminBriefing,
    type AdminBriefingKind,
    type AdminPersonalBriefing,
    type AdminGroupBriefing,
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
  import { channelLabel, derivedTitle } from '$lib/components/briefings/settings/briefingSettings';
  import { formatRelative } from '$lib/dateFormat';
  import { notifyError, notifySuccess } from '$lib/stores/notices';

  /** One row of either list, in the shape the table draws. */
  interface Row {
    kind: AdminBriefingKind;
    id: number;
    name: string;
    title: string;
    owner: string;
    /** Group owners, under the group's name; empty for a personal row. */
    owners: string;
    cron: string;
    timezone: string | null;
    delivery: string;
    state: 'running' | 'off' | 'held';
    lastRun: string | null;
  }

  let personal = $state<AdminPersonalBriefing[] | null>(null);
  let group = $state<AdminGroupBriefing[]>([]);
  let loadError = $state('');
  let filter = $state('');
  let busy = $state(false);
  let pausing = $state<Row | null>(null);
  let deleting = $state<Row | null>(null);
  let deleteStage = $state<'confirm' | 'step-up' | null>(null);

  function stateOf(enabled: boolean, held: unknown): Row['state'] {
    if (held) return 'held';
    return enabled ? 'running' : 'off';
  }

  function personalRow(b: AdminPersonalBriefing): Row {
    return {
      kind: 'personal',
      id: b.id,
      name: b.name,
      title: b.title || derivedTitle(b.name),
      owner: b.owner_label,
      owners: '',
      cron: b.cron,
      timezone: b.timezone,
      delivery: channelLabel(b.output),
      state: stateOf(b.enabled, b.held),
      lastRun: b.last_run_at,
    };
  }

  function groupRow(b: AdminGroupBriefing): Row {
    return {
      kind: 'group',
      id: b.id,
      name: b.name,
      title: b.title || derivedTitle(b.name),
      owner: b.group_label,
      owners: b.owners.join(', '),
      cron: b.cron,
      timezone: b.timezone,
      delivery: channelLabel(b.channel),
      state: stateOf(b.enabled, b.held),
      lastRun: b.last_published_at,
    };
  }

  function matches(row: Row, needle: string): boolean {
    if (!needle) return true;
    return [row.owner, row.owners, row.title, row.name].some((text) =>
      text.toLowerCase().includes(needle),
    );
  }

  const needle = $derived(filter.trim().toLowerCase());
  const personalRows = $derived(
    (personal ?? []).map(personalRow).filter((r) => matches(r, needle)),
  );
  const groupRows = $derived(group.map(groupRow).filter((r) => matches(r, needle)));

  const STATE_LABEL = { running: 'Running', off: 'Off', held: 'Paused by admin' } as const;
  const STATE_VARIANT = { running: 'success', off: 'neutral', held: 'warn' } as const;

  async function load() {
    try {
      const view = await getAdminBriefings();
      personal = view.personal;
      group = view.group;
      loadError = '';
    } catch (e) {
      if (e instanceof AuthError) {
        window.location.assign(`${base}/login`);
        return;
      }
      const message = e instanceof Error ? e.message : 'Briefings could not be loaded.';
      // A failed first load has nowhere else to go; a failed reload keeps the list.
      if (personal === null) loadError = message;
      else notifyError(message);
    }
  }
  onMount(load);

  function failed(e: unknown, fallback: string) {
    if (e instanceof AuthError) {
      window.location.assign(`${base}/login`);
      return;
    }
    if (e instanceof GroupApiError && e.code === 'migration_pending') {
      notifyError(
        'This briefing came from a migration that is still under review. Cut it over or ' +
          'roll it back with istota briefings migrate on the host, then delete it.',
      );
      return;
    }
    notifyError(e instanceof Error && e.message ? e.message : fallback);
  }

  async function pause(row: Row) {
    pausing = null;
    if (busy) return;
    busy = true;
    try {
      await holdAdminBriefing(row.kind, row.id);
    } catch (e) {
      failed(e, 'The briefing could not be paused.');
    } finally {
      await load();
      busy = false;
    }
  }

  async function resume(row: Row) {
    if (busy) return;
    busy = true;
    try {
      await resumeAdminBriefing(row.kind, row.id);
    } catch (e) {
      failed(e, 'The briefing could not be resumed.');
    } finally {
      await load();
      busy = false;
    }
  }

  function startDelete(row: Row) {
    deleting = row;
    deleteStage = 'confirm';
  }

  function stopDelete() {
    deleting = null;
    deleteStage = null;
  }

  async function submitDelete(proof: StepUpProof) {
    const row = deleting;
    if (!row) return;
    try {
      await deleteAdminBriefing(row.kind, row.id, row.name, proof);
      notifySuccess(`${row.title} deleted.`);
    } catch (e) {
      // A refused code or password stays in the step-up dialog, which reads `reason`.
      if (e instanceof GroupApiError && e.status === 403 && e.reason) throw e;
      failed(e, 'The briefing could not be deleted.');
    }
    stopDelete();
    await load();
  }

  function rowActions(row: Row): KebabItem[] {
    return [
      row.state === 'held'
        ? { label: 'Resume', disabled: busy, onSelect: () => resume(row) }
        : { label: 'Pause', disabled: busy, onSelect: () => (pausing = row) },
      { label: 'Delete', danger: true, disabled: busy, onSelect: () => startDelete(row) },
    ];
  }

  const deleteMessage = $derived(
    !deleting
      ? ''
      : deleting.kind === 'personal'
        ? `Delete ${deleting.title} for ${deleting.owner}? Its schedule and blocks are removed. ` +
          "Past editions stay in the owner's archive."
        : `Delete ${deleting.title} from ${deleting.owner}? It stops running and leaves the ` +
          "group's settings. Members keep its published editions.",
  );
</script>

<div class="settings admin-page card-pane">
  {#if personal === null && !loadError}
    <PageLoading label="Loading briefings…" />
  {:else if personal === null}
    <div class="center-msg error">{loadError}</div>
  {:else}
    <div class="filter-row">
      <Input
        type="search"
        aria-label="Filter briefings"
        placeholder="Filter by owner, group or title"
        bind:value={filter}
      />
    </div>
    {#snippet table(rows: Row[], label: string, ownerHeading: string)}
      <div class="table-scroll">
        <table class="grid briefings-grid" aria-label={label}>
          <thead
            ><tr
              ><th scope="col" class="col-owner">{ownerHeading}</th>
              <th scope="col">Briefing</th>
              <th scope="col" class="col-schedule">Schedule</th>
              <th scope="col" class="col-delivery">Delivery</th>
              <th scope="col" class="col-state">State</th>
              <th scope="col" class="col-last">Last run</th>
              <th scope="col" class="col-actions" aria-label="Actions"></th></tr
            ></thead
          >
          <tbody>
            {#each rows as row (row.id)}
              <tr>
                <td class="col-owner">
                  <div class="cell-main">{row.owner}</div>
                  {#if row.owners}<div class="caption">{row.owners}</div>{/if}
                </td>
                <td>
                  <div class="cell-main">{row.title}</div>
                  <div class="caption">{row.name}</div>
                </td>
                <td class="col-schedule">
                  <code>{row.cron}</code>
                  {#if row.timezone}<div class="caption">{row.timezone}</div>{/if}
                </td>
                <td class="col-delivery">{row.delivery}</td>
                <td class="col-state">
                  <Badge size="sm" variant={STATE_VARIANT[row.state]}
                    >{STATE_LABEL[row.state]}</Badge
                  >
                </td>
                <td class="col-last">{row.lastRun ? formatRelative(row.lastRun) : 'Never'}</td>
                <td class="col-actions">
                  <KebabMenu items={rowActions(row)} ariaLabel="Actions for {row.title}" />
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/snippet}

    <SettingsCard
      title="Personal briefings"
      description="A paused briefing does not run, and its owner cannot turn it back on until an administrator resumes it."
    >
      {#if personalRows.length}
        {@render table(personalRows, 'Personal briefings', 'Owner')}
      {:else if needle}
        <EmptyState size="sm">No personal briefings match the filter.</EmptyState>
      {:else}
        <EmptyState size="sm">No personal briefings yet.</EmptyState>
      {/if}
    </SettingsCard>

    <SettingsCard
      title="Group briefings"
      description="Deleting a group briefing removes it from the group's settings. Members keep its published editions."
    >
      {#if groupRows.length}
        {@render table(groupRows, 'Group briefings', 'Group')}
      {:else if needle}
        <EmptyState size="sm">No group briefings match the filter.</EmptyState>
      {:else}
        <EmptyState size="sm">No group briefings yet.</EmptyState>
      {/if}
    </SettingsCard>
  {/if}
</div>

<ConfirmDialog
  open={pausing !== null}
  title="Pause briefing"
  message={pausing
    ? `Pause ${pausing.title} for ${pausing.owner}? It will not run until an administrator resumes it.`
    : ''}
  confirmLabel="Pause"
  confirmVariant="primary"
  onConfirm={() => {
    if (pausing) void pause(pausing);
  }}
  onCancel={() => (pausing = null)}
/>

{#if deleting}
  <ConfirmDialog
    open={deleteStage === 'confirm'}
    title="Delete briefing"
    message={deleteMessage}
    confirmLabel="Delete"
    confirmVariant="danger"
    challenge={deleting.name}
    onConfirm={() => (deleteStage = 'step-up')}
    onCancel={stopDelete}
  />
{/if}

{#if deleteStage === 'step-up'}
  <StepUpDialog action="admin_briefing_delete" onConfirm={submitDelete} onCancel={stopDelete} />
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
  .briefings-grid .caption {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .briefings-grid code {
    font-size: var(--text-xs);
  }
  .col-owner {
    width: 9rem;
  }
  .col-schedule {
    width: 9rem;
  }
  .col-delivery {
    width: 5.5rem;
  }
  .col-state {
    width: 8.5rem;
  }
  .col-last {
    width: 6.5rem;
  }
  td.col-last {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .briefings-grid .col-actions {
    width: 2.5rem;
    text-align: right;
  }
  @container settings (max-width: 40rem) {
    .col-last,
    .col-delivery {
      display: none;
    }
    .col-owner,
    .col-schedule {
      width: 25%;
    }
  }
</style>
