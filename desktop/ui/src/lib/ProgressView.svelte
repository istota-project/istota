<script lang="ts">
  import { invoke } from '@tauri-apps/api/core';
  import { onMount } from 'svelte';
  import { elapsed, JOB_TITLES, type Status } from '$lib/status';

  let { status }: { status: Status } = $props();
  let now = $state(Date.now());
  let showLines = $state(false);

  onMount(() => {
    const timer = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(timer);
  });

  const progress = $derived(status.progress!);
  const failed = $derived(status.vm.state === 'error');
  const lines = $derived(
    status.vm.state === 'error' && status.vm.tail.length ? status.vm.tail : progress.lines,
  );
</script>

<h1>{JOB_TITLES[progress.job]}</h1>

{#if status.vm.state === 'setup'}
  <p>The setup page is open in Istota's window: enter your name, time zone and model there.</p>
  <div class="actions">
    <button class="primary" onclick={() => invoke('open_setup')}>Show the setup page</button>
  </div>
{/if}

<ol class="steps">
  {#each progress.steps as step (step.name)}
    <li
      class:done={step.finished_ms !== null}
      class:active={step.started_ms !== null && step.finished_ms === null}
    >
      <span>{step.name}</span>
      <span class="muted">
        {#if step.started_ms !== null && step.finished_ms !== null}
          {elapsed(step.started_ms, step.finished_ms)}
        {:else if step.started_ms !== null}
          {failed ? 'stopped here' : elapsed(step.started_ms, now)}
        {/if}
      </span>
    </li>
  {/each}
</ol>

{#if status.vm.state === 'error'}
  <section class="failure">
    <p>{status.vm.message}</p>
    {#if status.vm.command}
      <p class="muted">
        <code>{status.vm.command}</code>, exit status {status.vm.exit_code ?? 'none'}
      </p>
    {/if}
  </section>
{/if}

{#if lines.length}
  <button class="link" onclick={() => (showLines = !showLines)}>
    {showLines ? 'Hide' : 'Show'} the last lines
  </button>
  {#if showLines}
    <pre>{lines.join('\n')}</pre>
  {/if}
{/if}

<div class="actions">
  {#if status.resumable}
    <button class="primary" onclick={() => invoke('resume_setup')}>Resume</button>
    <button onclick={() => invoke('show_log_files')}>Show logs</button>
  {:else}
    <button onclick={() => invoke('cancel_setup')}>Cancel</button>
  {/if}
</div>

<style>
  .steps {
    list-style: none;
    padding: 0;
    margin: 16px 0;
  }

  .steps li {
    display: flex;
    justify-content: space-between;
    padding: 6px 0;
    border-bottom: 1px solid var(--border-default);
    color: var(--text-muted);
  }

  .steps li.active {
    color: var(--text-primary);
    font-weight: 600;
  }

  .steps li.done {
    color: var(--text-secondary);
  }
</style>
