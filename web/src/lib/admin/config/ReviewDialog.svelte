<script lang="ts">
  import { onMount } from 'svelte';
  import {
    ConfigChangeError,
    previewConfigChange,
    submitConfigChange,
    type ConfigChangeSet,
    type ConfigPreview,
    type ConfigProblem,
    type StepUpProof,
  } from '$lib/api';
  import { Badge, Button, ConfirmDialog, Modal } from '$lib/components/ui';
  import { StepUpDialog } from '$lib/components/settings';

  interface Props {
    changeSet: ConfigChangeSet;
    onClose: () => void;
    onSubmitted: (id: number) => void;
    /** config.toml changed under the page: reload the view, keep the draft. */
    onConflict: () => void;
    /** A request is already open; the page follows that one instead. */
    onRequestOpen: (id: number) => void;
  }

  let { changeSet, onClose, onSubmitted, onConflict, onRequestOpen }: Props = $props();

  /** The phrase a risky change is confirmed with. */
  const RISKY_CHALLENGE = 'apply';

  let stage = $state<'review' | 'confirm' | 'step-up'>('review');
  let preview = $state<ConfigPreview | null>(null);
  let loading = $state(true);
  let refusal = $state('');
  let refusalErrors = $state<string[]>([]);

  let blocked = $derived(
    !preview || preview.errors.length > 0 || preview.problems.blocking.length > 0,
  );
  let canApply = $derived(!!preview && !blocked && preview.changed);

  function label(problem: ConfigProblem): string {
    return problem.path.length ? problem.path.join('.') : 'config.toml';
  }

  function showRefusal(e: unknown) {
    if (e instanceof ConfigChangeError) {
      if (e.error === 'config_changed') {
        onConflict();
        return;
      }
      if (e.error === 'request_open' && typeof e.extra.id === 'number') {
        onRequestOpen(e.extra.id);
        return;
      }
      refusal = e.message;
      const errors = e.extra.errors;
      refusalErrors = Array.isArray(errors) ? errors.map(String) : [];
      if (e.error === 'managed' && Array.isArray(e.extra.resources)) {
        refusalErrors = [...refusalErrors, `Managed: ${e.extra.resources.join(', ')}`];
      }
      if (e.error === 'confirm_required' && Array.isArray(e.extra.keys)) {
        refusalErrors = [...refusalErrors, `Not confirmed: ${e.extra.keys.join(', ')}`];
      }
      return;
    }
    refusal = e instanceof Error ? e.message : 'The change could not be checked.';
    refusalErrors = [];
  }

  async function load() {
    loading = true;
    refusal = '';
    refusalErrors = [];
    try {
      preview = await previewConfigChange(changeSet);
    } catch (e) {
      preview = null;
      showRefusal(e);
    } finally {
      loading = false;
    }
  }

  function apply() {
    if (!canApply) return;
    stage = preview!.risky_keys.length ? 'confirm' : 'step-up';
  }

  async function submit(proof: StepUpProof) {
    try {
      const { id } = await submitConfigChange({
        ...changeSet,
        confirm: preview?.risky_keys ?? [],
        step_up: proof,
      });
      onSubmitted(id);
    } catch (e) {
      // A refused code stays in the code dialog, which reads `reason`.
      if (e instanceof ConfigChangeError && e.field === 'code') throw e;
      stage = 'review';
      showRefusal(e);
    }
  }

  onMount(load);
</script>

<Modal
  open={stage === 'review'}
  title="Review changes"
  width="720px"
  onOpenChange={(next) => {
    if (!next && stage === 'review') onClose();
  }}
>
  {#if loading}
    <p class="muted">Checking the change…</p>
  {:else}
    {#if refusal}
      <div class="banner danger" role="alert">
        <p>{refusal}</p>
        {#each refusalErrors as error, i (i)}<p>{error}</p>{/each}
      </div>
    {/if}

    {#if preview}
      {#if !preview.changed}
        <p class="muted">This changes nothing.</p>
      {/if}

      {#if preview.errors.length}
        <h3 class="micro-label">Fix before applying</h3>
        <ul class="problems">
          {#each preview.errors as error, i (i)}<li>{error}</li>{/each}
        </ul>
      {/if}

      {#if preview.problems.blocking.length}
        <h3 class="micro-label">Problems that block the change</h3>
        <ul class="problems">
          {#each preview.problems.blocking as problem, i (i)}
            <li>
              <code>{label(problem)}</code>
              {#if problem.pre_existing}<Badge size="sm" variant="warn">fix this first</Badge>{/if}
              <span>{problem.message}</span>
            </li>
          {/each}
        </ul>
        {#if preview.problems.blocking.some((p) => p.pre_existing)}
          <p class="caption">
            A problem marked "fix this first" is already in config.toml. The host refuses a file
            with it, so this change has to fix it too.
          </p>
        {/if}
      {/if}

      {#if preview.problems.warnings.length}
        <h3 class="micro-label">Warnings</h3>
        <ul class="problems">
          {#each preview.problems.warnings as problem, i (i)}
            <li><code>{label(problem)}</code> <span>{problem.message}</span></li>
          {/each}
        </ul>
      {/if}

      {#if preview.diff.length}
        <h3 class="micro-label">config.toml</h3>
        <pre class="diff">{preview.diff.join('\n')}</pre>
      {/if}

      {#if preview.secret_names.length}
        <p>Credentials changed: <code>{preview.secret_names.join(', ')}</code></p>
      {/if}
      {#if preview.admins_changed}
        <p>The admin list changes.</p>
      {/if}
      {#if preview.risky_keys.length}
        <p>
          These change who can sign in or how tasks are confined, and need a typed confirmation:
          <code>{preview.risky_keys.join(', ')}</code>
        </p>
      {/if}

      {#if preview.changed}
        <p class="restart-note">
          Applying restarts Istota.
          {#if preview.running_tasks === 1}
            One task is running now and will be interrupted.
          {:else if preview.running_tasks > 1}
            {preview.running_tasks} tasks are running now and will be interrupted.
          {:else}
            No task is running now.
          {/if}
        </p>
      {/if}
    {/if}
  {/if}

  {#snippet footer()}
    <Button variant="ghost" onclick={onClose}>Cancel</Button>
    <Button variant="secondary" onclick={load} disabled={loading}>Check again</Button>
    <Button variant="primary" onclick={apply} disabled={!canApply}>Apply…</Button>
  {/snippet}
</Modal>

{#if preview}
  <ConfirmDialog
    open={stage === 'confirm'}
    title="Confirm risky changes"
    confirmLabel="Continue"
    challenge={RISKY_CHALLENGE}
    message={`This change touches ${preview.risky_keys.join(', ')}. A wrong value can lock ` +
      'admins out or change how tasks are confined, and is fixed on the host.'}
    onConfirm={() => (stage = 'step-up')}
    onCancel={() => (stage = 'review')}
  />
{/if}

{#if stage === 'step-up'}
  <StepUpDialog
    action="admin_config_change"
    onConfirm={submit}
    onCancel={() => (stage = 'review')}
  />
{/if}

<style>
  .problems {
    margin: 0 0 var(--space-3);
    padding-left: var(--space-4);
    font-size: var(--text-sm);
  }

  .problems li {
    margin-bottom: var(--space-1);
  }

  .diff {
    margin: 0 0 var(--space-3);
    padding: var(--space-2);
    max-height: 20rem;
    overflow: auto;
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    background: var(--surface-base);
    border-radius: var(--radius-sm);
    white-space: pre;
  }

  .restart-note {
    font-weight: 600;
  }
</style>
