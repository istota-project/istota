<script lang="ts">
  import { invoke } from '@tauri-apps/api/core';
  import { onMount } from 'svelte';
  import type { CliTools, Place } from '$lib/status';

  let info = $state<CliTools | null>(null);
  let message = $state<string | null>(null);
  let working = $state(false);

  async function load() {
    info = await invoke<CliTools>('cli_tools_info');
  }

  onMount(load);

  async function run(command: string, place: Place) {
    message = null;
    working = true;
    try {
      const said = await invoke<string | null>(command, { place });
      message = said ?? null;
    } catch (err) {
      message = String(err);
    } finally {
      working = false;
      await load();
    }
  }
</script>

<p class="muted">
  <code>istota-stack</code> and <code>istota</code> in Terminal, for Istota on this Mac.
</p>
{#if info}
  <ul class="rows">
    {#each [{ place: 'system' as Place, dir: info.system_dir, installed: info.system, blocked: info.system_blocked, note: 'asks for an administrator password' }, { place: 'user' as Place, dir: info.user_dir, installed: info.user, blocked: info.user_blocked, note: 'for you alone' }] as row (row.place)}
      <li>
        <span>
          <code>{row.dir}</code>
          <span class="muted">{row.installed ? 'installed' : row.note}</span>
        </span>
        {#if row.installed}
          <button disabled={working} onclick={() => run('remove_cli_tools', row.place)}
            >Remove</button
          >
        {:else}
          <button
            disabled={working || row.blocked !== null}
            onclick={() => run('install_cli_tools', row.place)}
          >
            Install
          </button>
        {/if}
      </li>
      {#if row.blocked && !row.installed}
        <li class="blocked muted">{row.blocked}</li>
      {/if}
    {/each}
  </ul>
{/if}
{#if message}
  <p class="notice">{message}</p>
{/if}

<style>
  .rows {
    list-style: none;
    padding: 0;
    margin: 8px 0;
  }

  .rows li {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    padding: 6px 0;
    border-bottom: 1px solid var(--border-default);
  }

  .rows li.blocked {
    display: block;
    font-size: 12px;
  }

  .rows .muted {
    margin-left: 6px;
  }
</style>
