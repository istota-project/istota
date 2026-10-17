<script lang="ts">
  import { invoke } from '@tauri-apps/api/core';
  import { onMount } from 'svelte';
  import type { ProfileView } from '$lib/status';

  // `addOnly`: the first-run screen's "Connect to a remote Istota".
  let { addOnly = false }: { addOnly?: boolean } = $props();
  let profiles = $state<ProfileView[]>([]);
  let name = $state('');
  let url = $state('');
  let error = $state<string | null>(null);

  async function load() {
    profiles = await invoke<ProfileView[]>('profiles_info');
  }

  onMount(load);

  async function run(command: string, args: Record<string, unknown>) {
    error = null;
    try {
      await invoke(command, args);
      await load();
      return true;
    } catch (err) {
      error = String(err);
      return false;
    }
  }

  async function add() {
    if (await run('add_profile', { id: name, url })) {
      name = '';
      url = '';
    }
  }
</script>

{#if !addOnly}
  <ul class="rows">
    {#each profiles as profile (profile.id)}
      <li>
        <span>
          {profile.remote ? profile.id : 'This Mac'}
          {#if profile.url}<span class="muted">{profile.url}</span>{/if}
        </span>
        <span class="buttons">
          {#if profile.current}
            <span class="muted">open</span>
          {:else}
            <button onclick={() => run('select_profile', { id: profile.id })}>Open</button>
          {/if}
          {#if profile.remote}
            <button onclick={() => run('remove_profile', { id: profile.id })}>Remove</button>
          {/if}
        </span>
      </li>
    {/each}
  </ul>
{/if}

<form
  class="add"
  onsubmit={(e) => {
    e.preventDefault();
    add();
  }}
>
  <input type="text" placeholder="name, such as work" bind:value={name} size="14" />
  <input type="url" placeholder="https://istota.example.org/istota/" bind:value={url} size="30" />
  <button type="submit" disabled={!name || !url}>Connect</button>
</form>
<p class="muted small">
  Each remote Istota keeps its own sign-in and cookies in this app. Its address is https, or http on
  this Mac only.
</p>
{#if error}
  <p class="notice">{error}</p>
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

  .rows .muted {
    margin-left: 6px;
  }

  .buttons {
    display: flex;
    gap: 6px;
  }

  .add {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 8px;
  }

  input {
    background: var(--surface-raised);
    color: var(--text-primary);
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 6px 8px;
    font: inherit;
  }

  .small {
    font-size: 12px;
  }
</style>
