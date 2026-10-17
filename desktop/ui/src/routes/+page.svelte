<script lang="ts">
  import '../../../../web/src/lib/styles/tokens.css';
  import { invoke } from '@tauri-apps/api/core';
  import { listen } from '@tauri-apps/api/event';
  import { onMount } from 'svelte';
  import FirstRun from '$lib/FirstRun.svelte';
  import ProgressView from '$lib/ProgressView.svelte';
  import Settings from '$lib/Settings.svelte';
  import { firstRun, headline, making, type Status } from '$lib/status';

  let status = $state<Status | null>(null);
  let showLines = $state(false);
  let settingsOpen = $state(false);

  onMount(() => {
    invoke<Status>('app_status').then((s) => (status = s));
    const unlisten = [
      listen<Status>('status', (event) => (status = event.payload)),
      listen('show-settings', () => (settingsOpen = true)),
    ];
    return () => {
      for (const stop of unlisten) stop.then((f) => f());
    };
  });

  const busy = $derived(
    status !== null && ['unknown', 'starting', 'stopping', 'removing'].includes(status.vm.state),
  );
</script>

<main>
  {#if status}
    {#if status.notice}
      <p class="notice">{status.notice}</p>
    {/if}

    {#if making(status)}
      <ProgressView {status} />
    {:else if settingsOpen && !status.remote && status.vm.state !== 'absent'}
      <Settings {status} onclose={() => (settingsOpen = false)} />
    {:else if firstRun(status)}
      <FirstRun />
    {:else}
      <h1>{headline(status)}</h1>
      {#if busy}
        <p class="muted">This takes a moment.</p>
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
        {#if status.resumable}
          <button class="primary" onclick={() => invoke('resume_setup')}>Resume</button>
        {:else if status.vm.state === 'error' && status.vm.step === 'folder'}
          <button class="primary" onclick={() => invoke('locate_folder')}>Locate folder</button>
        {:else if status.vm.state === 'stopped' || status.vm.state === 'error'}
          <button onclick={() => invoke('vm_start')}>Start Istota</button>
        {/if}
        {#if !status.remote && status.vm.state !== 'absent'}
          <button onclick={() => (settingsOpen = true)}>Settings</button>
        {/if}
        <button onclick={() => invoke('show_log_files')}>Show logs</button>
        <button onclick={() => invoke('quit_app')}>Quit Istota</button>
      </div>
    {/if}
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

  :global(h1) {
    font-size: 20px;
    font-weight: 600;
    margin: 0 0 8px;
  }

  :global(.muted) {
    color: var(--text-muted);
  }

  :global(.notice) {
    border: 1px solid var(--border-default);
    background: var(--surface-card);
    padding: 8px 12px;
    border-radius: 6px;
  }

  :global(.failure) {
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

  :global(pre) {
    background: var(--surface-base);
    color: var(--text-secondary);
    padding: 8px;
    overflow: auto;
    max-height: 200px;
    font-size: 12px;
    white-space: pre-wrap;
  }

  :global(.actions) {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin-top: 20px;
  }

  :global(button) {
    background: var(--surface-raised);
    color: var(--text-primary);
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 6px 14px;
    font: inherit;
    cursor: pointer;
  }

  :global(button:hover) {
    border-color: var(--border-hover);
  }

  :global(button:disabled) {
    opacity: 0.5;
    cursor: default;
  }

  :global(button.primary) {
    background: var(--accent-amber-fill);
    color: var(--accent-amber-fill-fg);
    border-color: var(--accent-amber-fill);
  }

  :global(button.link) {
    background: none;
    border: none;
    padding: 0;
    color: var(--link);
  }
</style>
