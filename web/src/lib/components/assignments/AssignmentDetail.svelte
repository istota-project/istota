<script lang="ts">
  /**
   * One assignment's cards. Everything a wake wrote (summaries, hand-off text, evidence) is
   * drawn as a text node, never as markdown or a link: it may quote a page or a mail.
   */
  import { base } from '$app/paths';
  import {
    abandonAssignment,
    AuthError,
    completeAssignment,
    completeAssignmentHandoff,
    confirmAssignmentItem,
    isServerActionPath,
    noteAssignment,
    pauseAssignment,
    resumeAssignment,
    sendBackAssignment,
    topUpAssignment,
    wakeAssignment,
    type AssignmentDetail,
    type AssignmentItem,
    type AssignmentTopUpResult,
  } from '$lib/api';
  import { NetworkAccessCard } from '$lib/components/settings';
  import {
    ASSIGNMENT_STATE_LABEL,
    ASSIGNMENT_STATE_VARIANT,
    firedByText,
    isFinished,
    outcomeText,
    proposesCompletion,
    reasonText,
    steering,
    usd,
    wakeDueText,
  } from '$lib/settings/assignmentState';
  import { getCurrentUser } from '$lib/userContext';
  import {
    Alert,
    Badge,
    Button,
    Card,
    ConfirmDialog,
    Disclosure,
    Field,
    Input,
    Meter,
    TextArea,
    Timestamp,
    ToggleGroup,
  } from '@minot/ui';
  import { notifyError } from '@minot/ui/stores/notices';
  import { parseNumber } from '@minot/ui/ui/parse';

  let {
    detail,
    onChanged,
  }: {
    detail: AssignmentDetail;
    onChanged: () => void | Promise<void>;
  } = $props();

  const identity = getCurrentUser();

  const ITEM_STATUS = {
    open: { label: 'Open', variant: 'neutral' },
    passing: { label: 'Passing', variant: 'success' },
    failing: { label: 'Failing', variant: 'danger' },
    confirmed: { label: 'Confirmed', variant: 'success' },
  } as const;

  const assignment = $derived(detail.assignment);
  const ledger = $derived(detail.ledger);
  const can = $derived(steering(assignment));
  const reason = $derived(reasonText(assignment));
  const dueText = $derived(wakeDueText(detail.wake_due));
  const finished = $derived(isFinished(assignment.state));
  const maxCost = $derived(assignment.budget.max_cost_usd ?? 0);
  const maxWakes = $derived(assignment.budget.max_wakes ?? 0);

  let busy = $state(false);
  let abandonOpen = $state(false);
  let abandonReason = $state('');
  let missing = $state('');
  let missingError = $state('');
  let noteText = $state('');
  let noteError = $state('');
  let reports = $state<Record<number, string>>({});
  let reportErrors = $state<Record<number, string>>({});
  let topUpKind = $state<'usd' | 'wakes'>('usd');
  let topUpText = $state('');
  let topUpError = $state('');
  let stillPaused = $state<AssignmentTopUpResult['still_paused'] | null>(null);

  function statusOf(e: unknown): number | undefined {
    return (e as { status?: number }).status;
  }

  /** Runs one action, then reads again whatever it answered, since a refusal usually means
   *  the state moved. `besideField` takes a 400, a verdict on what was typed. */
  async function act(work: () => Promise<unknown>, besideField?: (message: string) => void) {
    if (busy) return false;
    busy = true;
    let done = false;
    try {
      await work();
      done = true;
    } catch (e) {
      const message = (e as Error).message || 'That did not work.';
      if (e instanceof AuthError) identity.expireSession();
      // The read below finds it gone and says so as it leaves.
      else if (statusOf(e) === 404) {
        /* nothing to add */
      } else if (statusOf(e) === 400 && besideField) besideField(message);
      else notifyError(message);
    } finally {
      await onChanged();
      busy = false;
    }
    return done;
  }

  async function confirmAbandon() {
    try {
      await abandonAssignment(assignment.id, abandonReason.trim());
    } catch (e) {
      if (e instanceof AuthError) {
        identity.expireSession();
        return;
      }
      if (statusOf(e) !== 404) throw e;
    }
    abandonReason = '';
    await onChanged();
  }

  async function sendBack() {
    missingError = '';
    const done = await act(
      () => sendBackAssignment(assignment.id, missing.trim()),
      (message) => (missingError = message),
    );
    if (done) missing = '';
  }

  async function markDone(actionId: number) {
    reportErrors[actionId] = '';
    const done = await act(
      () => completeAssignmentHandoff(assignment.id, actionId, (reports[actionId] ?? '').trim()),
      (message) => (reportErrors[actionId] = message),
    );
    if (done) delete reports[actionId];
  }

  async function sendNote(event: SubmitEvent) {
    event.preventDefault();
    noteError = '';
    const done = await act(
      () => noteAssignment(assignment.id, noteText.trim()),
      (message) => (noteError = message),
    );
    if (done) noteText = '';
  }

  async function topUp(event: SubmitEvent) {
    event.preventDefault();
    topUpError = '';
    stillPaused = null;
    const amount = parseNumber(topUpText, topUpKind === 'usd' ? 'float' : 'int');
    if (amount === null || amount <= 0) {
      topUpError = topUpKind === 'usd' ? 'Enter a number of dollars.' : 'Enter a number of wakes.';
      return;
    }
    const done = await act(
      async () => {
        const result = await topUpAssignment(
          assignment.id,
          topUpKind === 'usd' ? { usd: amount } : { wakes: amount },
        );
        stillPaused = result.still_paused ?? null;
      },
      (message) => (topUpError = message),
    );
    if (done) topUpText = '';
  }

  function canConfirm(item: AssignmentItem): boolean {
    return can.live && item.kind === 'confirmed' && item.status !== 'confirmed';
  }
</script>

<Card title="Now">
  {#snippet status()}
    <Badge size="sm" variant={ASSIGNMENT_STATE_VARIANT[assignment.state]}
      >{ASSIGNMENT_STATE_LABEL[assignment.state]}</Badge
    >
  {/snippet}
  {#if reason || dueText || (assignment.next_wake_at && !finished) || assignment.deadline || assignment.closed_at}
    <dl class="kv">
      {#if reason}
        <dt>Why</dt>
        <dd class="wake-text">{reason}</dd>
      {/if}
      {#if dueText}
        <dt>Wake</dt>
        <dd>{dueText}</dd>
      {/if}
      {#if assignment.next_wake_at && !finished}
        <dt>Next wake</dt>
        <dd><Timestamp value={assignment.next_wake_at} format="datetime" /></dd>
      {/if}
      {#if assignment.deadline}
        <dt>Deadline</dt>
        <dd><Timestamp value={assignment.deadline} format="datetime" /></dd>
      {/if}
      {#if assignment.closed_at}
        <dt>Closed</dt>
        <dd><Timestamp value={assignment.closed_at} format="datetime" /></dd>
      {/if}
    </dl>
  {/if}

  {#if assignment.state === 'draft'}
    <p>Continue this in chat to activate it.</p>
    <div>
      <Button
        size="sm"
        variant="secondary"
        href="{base}{detail.chat_href}"
        reload={isServerActionPath(detail.chat_href)}>Open chat</Button
      >
    </div>
  {/if}

  {#if proposesCompletion(assignment)}
    <h3>It says it is done</h3>
    {#if detail.completion_evidence}
      <p class="wake-text">{detail.completion_evidence}</p>
    {/if}
    <Field
      label="What is missing"
      description="Sent back to the assignment if you say it is not done."
      error={missingError}
    >
      <TextArea bind:value={missing} rows={2} invalid={Boolean(missingError)} />
    </Field>
    <div class="hstack wrap">
      <Button
        size="sm"
        variant="primary"
        disabled={busy}
        onclick={() => act(() => completeAssignment(assignment.id))}>Complete</Button
      >
      <Button size="sm" variant="secondary" disabled={busy || !missing.trim()} onclick={sendBack}
        >Send back</Button
      >
    </div>
  {/if}

  {#if can.pause || can.resume || can.wake || can.abandon}
    <div class="hstack wrap">
      {#if can.resume}
        <Button
          size="sm"
          variant="secondary"
          disabled={busy}
          onclick={() => act(() => resumeAssignment(assignment.id))}>Resume</Button
        >
      {/if}
      {#if can.wake}
        <Button
          size="sm"
          variant="secondary"
          disabled={busy}
          onclick={() => act(() => wakeAssignment(assignment.id))}>Wake now</Button
        >
      {/if}
      {#if can.pause}
        <Button
          size="sm"
          variant="secondary"
          disabled={busy}
          onclick={() => act(() => pauseAssignment(assignment.id))}>Pause</Button
        >
      {/if}
      {#if can.abandon}
        <Button size="sm" variant="danger" disabled={busy} onclick={() => (abandonOpen = true)}
          >Abandon</Button
        >
      {/if}
    </div>
  {/if}
</Card>

{#if detail.open_handoffs.length > 0}
  <Card title="Hand-offs">
    <ul class="divided-list">
      {#each detail.open_handoffs as handoff (handoff.id)}
        <li class="vstack handoff">
          <p class="wake-text">{handoff.text}</p>
          {#if handoff.report}
            <p class="caption wake-text">Report back: {handoff.report}</p>
          {/if}
          {#if handoff.due_at}
            <p class="caption">Due <Timestamp value={handoff.due_at} /></p>
          {/if}
          {#if !finished}
            <Field label="Your report" error={reportErrors[handoff.id]}>
              <TextArea
                bind:value={() => reports[handoff.id] ?? '', (next) => (reports[handoff.id] = next)}
                rows={2}
                invalid={Boolean(reportErrors[handoff.id])}
              />
            </Field>
            <div>
              <Button
                size="sm"
                variant="secondary"
                disabled={busy || !(reports[handoff.id] ?? '').trim()}
                onclick={() => markDone(handoff.id)}>Mark done</Button
              >
            </div>
          {/if}
        </li>
      {/each}
    </ul>
  </Card>
{/if}

{#if detail.items.length > 0}
  <Card title="Done when">
    <ul class="divided-list">
      {#each detail.items as item (item.id)}
        <li class="item-row">
          <div class="item-text">
            <p class="wake-text">{item.text}</p>
            {#if item.detail}<p class="caption wake-text">{item.detail}</p>{/if}
          </div>
          <div class="hstack">
            <Badge size="sm" variant={ITEM_STATUS[item.status].variant}
              >{ITEM_STATUS[item.status].label}</Badge
            >
            {#if canConfirm(item)}
              <Button
                size="sm"
                variant="secondary"
                disabled={busy}
                ariaLabel="Confirm {item.text}"
                onclick={() => act(() => confirmAssignmentItem(assignment.id, item.id))}
                >Confirm</Button
              >
            {/if}
          </div>
        </li>
      {/each}
    </ul>
  </Card>
{/if}

<Card title="Budget">
  <p class="caption">
    Counted in list-price dollars, the charter's own unit, so this is a budget and not what you were
    billed.
  </p>
  <div class="vstack budget-meters">
    <div class="vstack budget-line">
      <p class="num">{usd(ledger.cost_usd)} of {usd(maxCost)} list price</p>
      <Meter value={ledger.cost_usd} max={maxCost || 1} label="Cost used" />
    </div>
    <div class="vstack budget-line">
      <p class="num">{ledger.wakes} of {maxWakes} wakes</p>
      <Meter value={ledger.wakes} max={maxWakes || 1} label="Wakes used" />
    </div>
  </div>
  {#if ledger.unmeasured_wakes > 0}
    <p class="caption">
      {ledger.unmeasured_wakes === 1
        ? '1 wake could not be measured and counts'
        : `${ledger.unmeasured_wakes} wakes could not be measured and count`} as nothing here. The wake
      limit still applies.
    </p>
  {/if}
  {#if can.live}
    <form class="vstack" onsubmit={topUp} novalidate>
      <Field label="Top up">
        <ToggleGroup
          label="Top up"
          options={[
            { value: 'usd', label: 'Dollars' },
            { value: 'wakes', label: 'Wakes' },
          ]}
          bind:value={topUpKind}
          onValueChange={() => (topUpError = '')}
        />
      </Field>
      <Field label={topUpKind === 'usd' ? 'Dollars to add' : 'Wakes to add'} error={topUpError}>
        <Input
          bind:value={topUpText}
          inputmode={topUpKind === 'usd' ? 'decimal' : 'numeric'}
          autocomplete="off"
          invalid={Boolean(topUpError)}
        />
      </Field>
      {#if stillPaused}
        <Alert appearance="inline" variant="warn" role="status">
          It is still paused. That leaves {usd(stillPaused.remaining.cost_usd)} and
          {stillPaused.remaining.wakes} wakes{#if stillPaused.cheapest_wake_usd !== null}, and its
            cheapest wake so far cost {usd(stillPaused.cheapest_wake_usd)}{/if}.
        </Alert>
      {/if}
      <div>
        <Button type="submit" size="sm" variant="secondary" disabled={busy || !topUpText.trim()}
          >Top up</Button
        >
      </div>
    </form>
  {/if}
</Card>

{#if can.live}
  <Card title="Note">
    <form class="vstack" onsubmit={sendNote} novalidate>
      <Field
        label="Your note"
        description="The next wake reads it as your words, and it makes the next wake come sooner."
        error={noteError}
      >
        <TextArea bind:value={noteText} rows={3} invalid={Boolean(noteError)} />
      </Field>
      <div>
        <Button type="submit" size="sm" variant="secondary" disabled={busy || !noteText.trim()}
          >Send</Button
        >
      </div>
    </form>
  </Card>
{/if}

<Card title="Charter">
  {#snippet actions()}
    <Button
      size="sm"
      variant="secondary"
      href="{base}{detail.folder_href}"
      reload={isServerActionPath(detail.folder_href)}>Open folder</Button
    >
  {/snippet}
  <h3>Goal</h3>
  <p class="wake-text">{detail.goal}</p>
  <h3>What it may do without asking</h3>
  <p class="wake-text">{detail.autonomy}</p>
  {#if detail.frozen.length > 0}
    <h3>Frozen commitments</h3>
    <dl class="kv">
      {#each detail.frozen as commitment (commitment.id)}
        <dt class="wake-text">{commitment.name}</dt>
        <dd class="wake-text">{commitment.value}</dd>
      {/each}
    </dl>
  {/if}
  {#if assignment.deadline}
    <h3>Deadline</h3>
    <p><Timestamp value={assignment.deadline} format="datetime" /></p>
  {/if}
</Card>

{#if detail.wakes.length > 0}
  <Card title="Recent wakes">
    <p class="caption">{detail.notice}</p>
    <ul class="divided-list">
      {#each detail.wakes as wake, index (wake.seq)}
        <li class="vstack wake">
          <div class="hstack wrap wake-head">
            <span class="title-card num">Wake {wake.seq}</span>
            {#if wake.started_at}
              <Timestamp class="caption" value={wake.started_at} format="datetime" />
            {/if}
          </div>
          <p class="caption">
            {firedByText(wake.fired_by)}. {outcomeText(wake.outcome)}. {wake.cost_usd === null
              ? 'Cost not measured'
              : `${usd(wake.cost_usd)} list price`}.
          </p>
          {#if wake.summary}
            <Disclosure summary="Summary" open={index === 0}>
              <p class="wake-text">{wake.summary}</p>
              {#if wake.summary_truncated}
                <p class="caption">Cut short here.</p>
              {/if}
            </Disclosure>
          {/if}
        </li>
      {/each}
    </ul>
  </Card>
{/if}

{#if detail.network}
  <NetworkAccessCard
    title="Network access"
    subject={detail.network}
    showJobText={false}
    externalSkills={detail.external_skills}
    {onChanged}
  />
{/if}

<ConfirmDialog
  bind:open={abandonOpen}
  title="Abandon this assignment"
  confirmLabel="Abandon"
  busyLabel="Abandoning…"
  confirmVariant="danger"
  onConfirm={confirmAbandon}
>
  {#snippet body()}
    <div class="vstack">
      <p>This stops {assignment.title} for good. Its folder and its record stay.</p>
      <Field label="Reason" optional>
        <TextArea bind:value={abandonReason} rows={2} />
      </Field>
    </div>
  {/snippet}
</ConfirmDialog>

<style>
  .wake-text {
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .handoff,
  .wake {
    --vstack-gap: var(--space-2);
  }
  .budget-line {
    --vstack-gap: var(--space-1);
  }
  .item-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
  }
  .item-text {
    flex: 1 1 12rem;
    min-width: 0;
  }
  .wake-head {
    justify-content: space-between;
  }
</style>
