<script lang="ts">
  import {
    addNetworkGrant,
    approveNetworkJob,
    AuthError,
    dismissNetworkRefusal,
    removeNetworkGrant,
    type NetworkSubject,
  } from '$lib/api';
  import { getCurrentUser } from '$lib/userContext';
  import { Alert, Badge, Button, Card, ConfirmDialog, Disclosure, Field, Input } from '@minot/ui';
  import { notifyError } from '@minot/ui/stores/notices';
  import { createConfirm } from '@minot/ui/ui/confirm.svelte';

  let {
    subject,
    externalSkills,
    onChanged,
  }: {
    subject: NetworkSubject;
    externalSkills: string[];
    onChanged: () => void | Promise<void>;
  } = $props();

  const identity = getCurrentUser();

  // Each of these reaches past anything a host list bounds: the browser and the
  // devbox send wherever a page or a command says.
  const UNBOUNDED_SKILLS = new Set(['browse', 'devbox']);
  const STATES = {
    open: { label: 'Full access', variant: 'warn' },
    public: { label: 'Public', variant: 'info' },
    limited: { label: 'Limited', variant: 'neutral' },
  } as const;
  const JOB_REQUESTS = new Set(['open', 'public']);

  const job = $derived(subject.job);
  const stateBadge = $derived(STATES[subject.state]);
  const hostRefusals = $derived(subject.refused.filter((row) => !JOB_REQUESTS.has(row.entry)));
  const asksOpen = $derived(subject.refused.some((row) => row.entry === 'open'));
  const asksPublic = $derived(subject.refused.some((row) => row.entry === 'public'));
  const offeredSkills = $derived(externalSkills.filter((name) => !subject.skills.includes(name)));
  const canApprovePublic = $derived(
    job !== null && job.reach === 'public' && subject.state !== 'public',
  );
  const canApproveOpen = $derived(job !== null && subject.state !== 'open');
  const openEntry = $derived(subject.approvals.find((entry) => entry.startsWith('open:')));
  const publicEntry = $derived(subject.approvals.find((entry) => entry.startsWith('public:')));

  let host = $state('');
  let hostError = $state('');
  let busy = $state(false);

  const grantSkill = createConfirm<string>();
  const approve = createConfirm<'open' | 'public'>();

  function failed(e: unknown) {
    if (e instanceof AuthError) identity.expireSession();
    else notifyError((e as Error).message || 'That could not be saved.');
  }

  async function run(work: () => Promise<unknown>) {
    if (busy) return;
    busy = true;
    try {
      await work();
      await onChanged();
    } catch (e) {
      failed(e);
    } finally {
      busy = false;
    }
  }

  function skillOf(entry: string): string | null {
    return entry.startsWith('skill:') ? entry.slice('skill:'.length) : null;
  }

  function add(entry: string) {
    const skill = skillOf(entry);
    if (skill !== null && UNBOUNDED_SKILLS.has(skill)) {
      grantSkill.ask(skill);
      return;
    }
    void run(() => addNetworkGrant(subject.subject, entry));
  }

  async function addHost(event: SubmitEvent) {
    event.preventDefault();
    const entry = host.trim();
    if (!entry || busy) return;
    busy = true;
    hostError = '';
    try {
      await addNetworkGrant(subject.subject, entry);
      host = '';
      await onChanged();
    } catch (e) {
      if (e instanceof AuthError) identity.expireSession();
      else if ((e as { field?: string }).field === 'entry') hostError = (e as Error).message;
      else notifyError((e as Error).message || 'That could not be saved.');
    } finally {
      busy = false;
    }
  }

  async function confirmSkill() {
    const skill = grantSkill.target;
    if (!skill) return;
    await addNetworkGrant(subject.subject, `skill:${skill}`);
    await onChanged();
  }

  async function confirmApproval() {
    const kind = approve.target;
    if (!kind || !job) return;
    try {
      await approveNetworkJob(subject.subject, kind, job.digest);
    } catch (e) {
      // The text may have moved. Show what it says now under the refusal.
      await onChanged();
      throw e;
    }
    await onChanged();
  }
</script>

{#snippet jobText()}
  {#if job}
    {#if job.prompt}<pre class="job-text">{job.prompt}</pre>{/if}
    {#if job.command}<pre class="job-text">{job.command}</pre>{/if}
    {#if job.skill}<pre class="job-text">{job.skill} {job.skill_args}</pre>{/if}
    {#if job.publish_shared_kv}
      <p>
        Publishes its answer to shared content other users' briefings can read, as <code
          >{job.publish_shared_kv}</code
        >{job.publish_shared_kv_trusted ? ', marked trusted' : ''}.
      </p>
    {/if}
  {/if}
{/snippet}

<Card title={subject.label}>
  {#snippet status()}<Badge size="sm" variant={stateBadge.variant}>{stateBadge.label}</Badge
    >{/snippet}

  {#if subject.state === 'open'}
    <p class="caption">
      This job runs as it did before, with your browser profile and every skill, and can reach any
      public host.
    </p>
  {:else if subject.state === 'public'}
    <p class="caption">This job has open web and holds nothing private.</p>
  {:else}
    <p class="caption">
      This reaches only what is listed here and the services Istota is set up with.
    </p>
  {/if}

  {#if job}
    <Disclosure summary="Job text">{@render jobText()}</Disclosure>
  {/if}

  {#if asksOpen}<p>This job asked for full access.</p>{/if}
  {#if asksPublic}<p>This job asks for public access.</p>{/if}

  {#if asksOpen || asksPublic || canApproveOpen || canApprovePublic || openEntry || publicEntry}
    <div class="hstack wrap">
      {#if openEntry}
        <Button
          variant="secondary"
          size="sm"
          disabled={busy}
          onclick={() => run(() => removeNetworkGrant(subject.subject, openEntry))}
          >Remove full access</Button
        >
      {:else if canApproveOpen}
        <Button variant="secondary" size="sm" onclick={() => approve.ask('open')}
          >Approve full access</Button
        >
      {/if}
      {#if publicEntry}
        <Button
          variant="secondary"
          size="sm"
          disabled={busy}
          onclick={() => run(() => removeNetworkGrant(subject.subject, publicEntry))}
          >Remove public access</Button
        >
      {:else if canApprovePublic}
        <Button variant="secondary" size="sm" onclick={() => approve.ask('public')}
          >Approve public access</Button
        >
      {/if}
      {#each subject.refused.filter((row) => JOB_REQUESTS.has(row.entry)) as row (row.entry)}
        <Button
          variant="secondary"
          size="sm"
          disabled={busy}
          ariaLabel="Dismiss {row.entry}"
          onclick={() => run(() => dismissNetworkRefusal(subject.subject, row.entry))}
          >Dismiss request</Button
        >
      {/each}
    </div>
  {/if}

  {#if hostRefusals.length > 0}
    <h3>Recently refused</h3>
    <Alert appearance="inline" variant="warn">
      A page the job reads can make it ask for a host. Add one only if you expect this job to use
      it.
    </Alert>
    <ul class="divided-list">
      {#each hostRefusals as row (row.entry)}
        <li class="entry-row">
          <span class="entry-name mono">{row.entry}</span>
          <span class="hstack">
            <Button
              size="sm"
              variant="secondary"
              disabled={busy}
              ariaLabel="Add {row.entry}"
              onclick={() => add(row.entry)}>Add</Button
            >
            <Button
              size="sm"
              variant="secondary"
              disabled={busy}
              ariaLabel="Dismiss {row.entry}"
              onclick={() => run(() => dismissNetworkRefusal(subject.subject, row.entry))}
              >Dismiss</Button
            >
          </span>
        </li>
      {/each}
    </ul>
  {/if}

  {#if subject.hosts.length > 0 || subject.skills.length > 0}
    <h3>Approved</h3>
    <ul class="divided-list">
      {#each subject.hosts as entry (entry)}
        <li class="entry-row">
          <span class="entry-name mono">{entry}</span>
          <Button
            size="sm"
            variant="secondary"
            disabled={busy}
            ariaLabel="Remove {entry}"
            onclick={() => run(() => removeNetworkGrant(subject.subject, entry))}>Remove</Button
          >
        </li>
      {/each}
      {#each subject.skills as name (name)}
        <li class="entry-row">
          <span class="entry-name mono">{name}</span>
          <Button
            size="sm"
            variant="secondary"
            disabled={busy}
            ariaLabel="Remove skill {name}"
            onclick={() => run(() => removeNetworkGrant(subject.subject, `skill:${name}`))}
            >Remove</Button
          >
        </li>
      {/each}
    </ul>
  {/if}

  <form class="vstack" onsubmit={addHost} novalidate>
    <Field
      label="Add a host"
      description="An exact name such as news.example.com. It also serves anyone else's account on a shared platform."
      error={hostError}
    >
      <Input bind:value={host} monospace autocomplete="off" invalid={Boolean(hostError)} />
    </Field>
    <div>
      <Button type="submit" size="sm" variant="secondary" disabled={busy || !host.trim()}
        >Add host</Button
      >
    </div>
  </form>

  {#if offeredSkills.length > 0}
    <Disclosure summary="Add a skill">
      <div class="hstack wrap">
        {#each offeredSkills as name (name)}
          <Button
            size="sm"
            variant="secondary"
            disabled={busy}
            ariaLabel="Add skill {name}"
            onclick={() => add(`skill:${name}`)}>{name}</Button
          >
        {/each}
      </div>
    </Disclosure>
  {/if}
</Card>

<ConfirmDialog
  bind:open={grantSkill.open}
  title="Grant {grantSkill.target ?? 'this skill'}"
  message="This grant is unbounded. With {grantSkill.target ??
    'this skill'}, the job can send what it holds to any host a page or a command names, and no host list applies."
  confirmLabel="Grant"
  busyLabel="Granting…"
  confirmVariant="danger"
  onConfirm={confirmSkill}
/>

<ConfirmDialog
  bind:open={approve.open}
  title={approve.target === 'public' ? 'Approve public access' : 'Approve full access'}
  confirmLabel="Approve"
  busyLabel="Approving…"
  confirmVariant="danger"
  onConfirm={confirmApproval}
>
  {#snippet body()}
    <div class="vstack">
      {#if approve.target === 'public'}
        <p>
          This exact text runs with open web and nothing private: no memory, no files and no room
          notes. Editing the text drops this approval.
        </p>
      {:else}
        <p>
          Full access is unbounded. This exact text runs with your browser profile and every skill,
          and can send what it reads to any public host. Editing the text drops this approval.
        </p>
      {/if}
      {@render jobText()}
    </div>
  {/snippet}
</ConfirmDialog>

<style>
  .entry-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
  }

  .entry-name {
    min-width: 0;
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
</style>
