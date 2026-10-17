<script lang="ts">
  import '../../../../web/src/lib/styles/tokens.css';
  import { invoke } from '@tauri-apps/api/core';
  import { listen } from '@tauri-apps/api/event';
  import { onMount } from 'svelte';
  import { headline, type Status } from '$lib/status';

  let status = $state<Status | null>(null);
  let showLines = $state(false);

  onMount(() => {
    invoke<Status>('app_status').then((s) => (status = s));
    const unlisten = listen<Status>('status', (event) => (status = event.payload));
    return () => {
      unlisten.then((stop) => stop());
    };
  });

  const busy = $derived(
    status !== null &&
      ['unknown', 'creating', 'loading', 'starting', 'stopping'].includes(status.vm.state),
  );
</script>

<main>
  {#if status}
    <h1>{headline(status)}</h1>
    {#if busy}
      <p class="muted">This takes a moment.</p>
    {/if}

    {#if status.notice}
      <p class="notice">{status.notice}</p>
    {/if}

    {#if status.vm.state === 'error'}
      <section class="failure">
        <p>{status.vm.message}</p>
        <dl>
          <dt>Step</dt>
          <dd>{status.vm.step}</dd>
          {#if status.vm.command}
            <dt>Command</dt>
            <dd><code>{status.vm.command}</code></dd>
          {/if}
          {#if status.vm.exit_code !== null}
            <dt>Exit status</dt>
            <dd>{status.vm.exit_code}</dd>
          {/if}
        </dl>
        {#if status.vm.tail.length}
          <button class="link" onclick={() => (showLines = !showLines)}>
            {showLines ? 'Hide' : 'Show'} the last lines
          </button>
          {#if showLines}
            <pre>{status.vm.tail.join('\n')}</pre>
          {/if}
        {/if}
      </section>
    {/if}

    <div class="actions">
      {#if status.remote || (status.vm.state === 'running' && status.healthy !== false)}
        <button class="primary" onclick={() => invoke('open_web')}>Open Istota</button>
      {/if}
      {#if status.vm.state === 'setup'}
        <button class="primary" onclick={() => invoke('open_setup')}>Continue setup</button>
      {/if}
      {#if status.vm.state === 'stopped' || status.vm.state === 'error'}
        <button onclick={() => invoke('vm_start')}>Start Istota</button>
      {/if}
      <button onclick={() => invoke('show_log_files')}>Show logs</button>
      <button onclick={() => invoke('quit_app')}>Quit Istota</button>
    </div>
  {/if}
</main>

<style>
  :global(body) {
    margin: 0;
    background: var(--surface-base);
    color: var(--text-primary);
    font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif;
    font-size: 14px;
  }

  main {
    padding: 24px 28px;
  }

  h1 {
    font-size: 20px;
    font-weight: 600;
    margin: 0 0 8px;
  }

  .muted {
    color: var(--text-muted);
    margin: 0 0 16px;
  }

  .notice {
    border: 1px solid var(--border-default);
    background: var(--surface-card);
    padding: 8px 12px;
    border-radius: 6px;
  }

  .failure {
    border: 1px solid var(--accent-amber);
    background: var(--surface-card);
    padding: 12px 14px;
    border-radius: 6px;
    margin: 12px 0;
  }

  dl {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 4px 12px;
    margin: 8px 0;
  }

  dt {
    color: var(--text-muted);
  }

  dd {
    margin: 0;
  }

  pre {
    background: var(--surface-base);
    color: var(--text-secondary);
    padding: 8px;
    overflow: auto;
    max-height: 200px;
    font-size: 12px;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 20px;
  }

  button {
    background: var(--surface-raised);
    color: var(--text-primary);
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 6px 14px;
    font: inherit;
    cursor: pointer;
  }

  button:hover {
    border-color: var(--border-hover);
  }

  button.primary {
    background: var(--accent-amber-fill);
    color: var(--accent-amber-fill-fg);
    border-color: var(--accent-amber-fill);
  }

  button.link {
    background: none;
    border: none;
    padding: 0;
    color: var(--link);
  }
</style>
