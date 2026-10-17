<script lang="ts">
  import { invoke } from '@tauri-apps/api/core';
  import { onMount } from 'svelte';
  import CliTools from '$lib/CliTools.svelte';
  import Profiles from '$lib/Profiles.svelte';
  import { LAYER_NAMES, type SettingsInfo, type Status } from '$lib/status';

  let { status, onclose }: { status: Status; onclose: () => void } = $props();
  // The sections about the VM on this Mac, once there is one.
  const local = $derived(!status.remote && status.vm.state !== 'absent');
  let info = $state<SettingsInfo | null>(null);
  let date = $state('');
  let error = $state<string | null>(null);

  async function load() {
    info = await invoke<SettingsInfo>('settings_info');
  }

  onMount(() => {
    if (local) load();
  });

  async function run(command: string, args: Record<string, unknown> = {}) {
    error = null;
    try {
      await invoke(command, args);
    } catch (err) {
      error = String(err);
    }
  }

  async function togglePort(port: number, allow: boolean) {
    await run('set_host_port', { port, allow });
    setTimeout(load, 3000);
  }
</script>

<header>
  <h1>Settings</h1>
  <button onclick={onclose}>Done</button>
</header>

<p class="muted">
  The model, its key and your other credentials are in Istota's own settings, in its window.
</p>

{#if error}
  <p class="notice">{error}</p>
{/if}

<section>
  <h2>Profiles</h2>
  <Profiles />
</section>

<section>
  <h2>Command-line tools</h2>
  <CliTools />
</section>

{#if local}
  <section>
    <h2>Background installs</h2>
    <p class="muted">
      Claude Code and Google Chrome may not be shipped inside the app, so Istota installs them on
      this Mac after setup. Istota works without them.
    </p>
    {#if status.layers.length}
      <ul class="rows">
        {#each status.layers as layer (layer.layer)}
          <li>
            <span>{LAYER_NAMES[layer.layer] ?? layer.layer}</span>
            <span class="state {layer.state}">{layer.state}</span>
          </li>
          {#if layer.state === 'failed' && layer.detail}
            <pre>{layer.detail}</pre>
          {/if}
        {/each}
      </ul>
    {:else}
      <p class="muted">Not checked yet: Istota checks once it is running.</p>
    {/if}
    {#if status.layers_hint}
      <p class="notice">{status.layers_hint}</p>
    {/if}
    <div class="actions">
      <button
        disabled={status.layers_running || !info?.running}
        onclick={() => run('retry_layers')}
      >
        {status.layers_running ? 'Installing…' : 'Retry'}
      </button>
      {#if info?.running}
        {#if info.claude_code_wanted}
          <button onclick={() => run('set_claude_code', { wanted: false }).then(load)}>
            Don't install Claude Code
          </button>
        {:else}
          <button onclick={() => run('set_claude_code', { wanted: true }).then(load)}>
            Install Claude Code
          </button>
        {/if}
      {/if}
    </div>
  </section>

  <section>
    <h2>Local models</h2>
    <p class="muted">
      A model server on this Mac is reachable from Istota only when you allow its port.
    </p>
    {#if info}
      <ul class="rows">
        {#each info.models as model (model.port)}
          <li>
            <span>
              {model.name}
              <span class="muted"
                >port {model.port}, {model.listening ? 'running' : 'not running'}</span
              >
            </span>
            <label>
              <input
                type="checkbox"
                checked={model.allowed}
                disabled={!info.running}
                onchange={(e) => togglePort(model.port, e.currentTarget.checked)}
              />
              Allow
            </label>
          </li>
        {/each}
      </ul>
    {/if}
  </section>

  <section>
    <h2>Restore from backup</h2>
    <p class="muted">
      Makes Istota again from the backups in {info?.workspace ?? 'its folder'}. Leave the date empty
      for the newest backup.
    </p>
    <div class="actions">
      <input type="text" placeholder="2026-10-01" bind:value={date} size="12" />
      <button onclick={() => run('restore_backup', { date: date || null })}>Restore…</button>
    </div>
  </section>

  <section>
    <h2>Remove Istota</h2>
    <p class="muted">
      Deletes Istota's VM on this Mac. Your folder, its backups and the master key in your login
      Keychain stay.
    </p>
    <div class="actions">
      <button class="danger" onclick={() => run('remove_istota')}>Remove Istota…</button>
    </div>
  </section>
{/if}

<style>
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  section {
    margin-top: 20px;
  }

  h2 {
    font-size: 15px;
    font-weight: 600;
    margin: 0 0 4px;
  }

  .rows {
    list-style: none;
    padding: 0;
    margin: 8px 0;
  }

  .rows li {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 6px 0;
    border-bottom: 1px solid var(--border-default);
  }

  .state.failed {
    color: var(--accent-amber);
  }

  input[type='text'] {
    background: var(--surface-raised);
    color: var(--text-primary);
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 6px 8px;
    font: inherit;
  }

  button.danger {
    border-color: var(--accent-amber);
  }
</style>
