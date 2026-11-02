<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { base } from '$app/paths';
  import {
    AuthError,
    getAssignment,
    getAssignments,
    type AssignmentDetail as Detail,
    type AssignmentListRow,
  } from '$lib/api';
  import AssignmentDetail from '$lib/components/assignments/AssignmentDetail.svelte';
  import { SettingsLayout } from '$lib/components/settings';
  import { createUrlSelection } from '$lib/navigation/urlSelection.svelte';
  import {
    ASSIGNMENT_STATE_LABEL,
    ASSIGNMENT_STATE_VARIANT,
    budgetLine,
    budgetUsedPercent,
    isFinished,
    reasonText,
  } from '$lib/settings/assignmentState';
  import { getCurrentUser } from '$lib/userContext';
  import { Badge, Card, Disclosure, EmptyState, Meter, PageHeader, Timestamp } from '@minot/ui';
  import { notifyError, notifyWarning } from '@minot/ui/stores/notices';
  import { latest } from '@minot/ui/ui/latest';
  import { onDocumentVisible } from '@minot/ui/ui/visibility';

  const LIST_HREF = `${base}/settings/assignments`;
  const CHAT_HREF = `${base}/chat`;
  const CHAT_NOTE =
    'Assignments are started and changed in chat. Here you can see the active ones and steer them.';
  const GONE = 'This assignment is not available.';
  const FALLBACK_LOAD_ERROR = 'Assignments could not be loaded.';

  const identity = getCurrentUser();

  let rows = $state<AssignmentListRow[] | null>(null);
  let detail = $state<Detail | null>(null);
  let selectedId = $state<number | null>(null);
  // The gate turned this owner away. A bell row still lands here, so it is a state, not a failure.
  let unavailable = $state(false);
  let loadError = $state('');
  let loading = $state(true);

  const loads = latest();
  onDestroy(() => loads.dispose());

  const active = $derived((rows ?? []).filter((row) => !isFinished(row.state)));
  const finished = $derived((rows ?? []).filter((row) => isFinished(row.state)));
  const shownDetail = $derived(detail && detail.assignment.id === selectedId ? detail : null);
  const waitingForDetail = $derived(selectedId !== null && !shownDetail && !unavailable);

  const selection = createUrlSelection<{ assignment: number | null }>({
    key: 'settingsAssignment',
    params: ['assignment'],
    encode: ({ assignment }): Record<string, string> =>
      assignment === null ? {} : { assignment: String(assignment) },
    decode: (params) => {
      const text = params.assignment ?? '';
      return { assignment: /^\d+$/.test(text) ? Number(text) : null };
    },
    read: () => (rows ? { assignment: selectedId } : null),
    apply: ({ assignment }) => {
      selectedId = assignment;
      void refresh();
    },
  });
  selection.start();

  function statusOf(e: unknown): number | undefined {
    return (e as { status?: number }).status;
  }

  /** The assignment on screen can go: the gate can close, or the link named someone else's. */
  function leaveGone() {
    selection.replace({ assignment: null });
    selectedId = null;
    detail = null;
    notifyWarning(GONE);
  }

  /** `quiet` is a read nobody asked for, which says nothing when it fails. */
  async function refresh({ quiet = false } = {}) {
    const current = loads.next();
    try {
      const listing = await getAssignments();
      if (!current()) return;
      const first = rows === null;
      rows = listing.assignments;
      unavailable = false;
      loadError = '';
      if (first) selectedId = selection.current()?.assignment ?? null;
      if (selectedId === null) {
        detail = null;
        return;
      }
      const one = await getAssignment(selectedId);
      if (current()) detail = one;
    } catch (e) {
      if (!current()) return;
      if (e instanceof AuthError) identity.expireSession();
      else if (statusOf(e) === 404 && rows === null) unavailable = true;
      else if (statusOf(e) === 404) {
        // Asked of the whole list too, in case the gate closed while the page was open.
        if (selectedId !== null) leaveGone();
        else unavailable = true;
      } else if (quiet) return;
      else if (shownDetail || (rows && selectedId === null))
        notifyError((e as Error).message || FALLBACK_LOAD_ERROR);
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

  onMount(() => {
    void refresh();
    return onDocumentVisible(() => void refresh({ quiet: true }));
  });
</script>

{#snippet chatNote()}
  <p class="caption">{CHAT_NOTE} <a href={CHAT_HREF}>Open chat</a></p>
{/snippet}

{#snippet listRow(row: AssignmentListRow)}
  {@const reason = reasonText(row)}
  <li class="assignment-row vstack">
    <div class="hstack wrap">
      <a class="link-quiet title-card assignment-title" href="{LIST_HREF}?assignment={row.id}"
        >{row.title}</a
      >
      <Badge size="sm" variant={ASSIGNMENT_STATE_VARIANT[row.state]}
        >{ASSIGNMENT_STATE_LABEL[row.state]}</Badge
      >
      {#if row.open_handoffs > 0}
        <Badge size="sm" variant="info" uppercase={false}>
          {row.open_handoffs === 1 ? '1 step for you' : `${row.open_handoffs} steps for you`}
        </Badge>
      {/if}
    </div>
    {#if reason}<p class="caption assignment-text">{reason}</p>{/if}
    {#if isFinished(row.state)}
      {#if row.closed_at}
        <p class="caption">Closed <Timestamp value={row.closed_at} format="datetime" /></p>
      {/if}
    {:else if row.state === 'draft'}
      <p class="caption">Not started yet. Continue it in chat to activate it.</p>
    {:else if row.next_wake_at}
      <p class="caption">Next wake <Timestamp value={row.next_wake_at} format="datetime" /></p>
    {/if}
    {#if !isFinished(row.state)}
      <Meter value={budgetUsedPercent(row, row.ledger)} label="Budget used" />
    {/if}
    <p class="caption num">{budgetLine(row, row.ledger)}</p>
  </li>
{/snippet}

<SettingsLayout loading={loading || (waitingForDetail && !loadError)} {loadError} onRetry={retry}>
  {#if unavailable}
    <EmptyState>Assignments are not available on your account.</EmptyState>
  {:else if shownDetail}
    {@const assignment = shownDetail.assignment}
    <PageHeader
      title={assignment.title}
      back={{ href: LIST_HREF, label: 'Assignments' }}
      align="start"
    />
    {@render chatNote()}
    {#key assignment.id}
      <AssignmentDetail detail={shownDetail} onChanged={() => refresh()} />
    {/key}
  {:else if rows}
    {@render chatNote()}
    {#if rows.length === 0}
      <EmptyState>You have no assignments yet. Start one in chat.</EmptyState>
    {:else}
      {#if active.length > 0}
        <Card title="Active">
          <ul class="divided-list">
            {#each active as row (row.id)}
              {@render listRow(row)}
            {/each}
          </ul>
        </Card>
      {/if}
      {#if finished.length > 0}
        <Disclosure summary="Finished ({finished.length})">
          <ul class="divided-list">
            {#each finished as row (row.id)}
              {@render listRow(row)}
            {/each}
          </ul>
        </Disclosure>
      {/if}
    {/if}
  {/if}
</SettingsLayout>

<style>
  .assignment-row {
    --vstack-gap: var(--space-2);
  }
  /* The finished rows sit outside a Card, which is what resets a paragraph's margin. */
  .assignment-row p {
    margin: 0;
  }
  .assignment-title,
  .assignment-text {
    overflow-wrap: anywhere;
  }
</style>
