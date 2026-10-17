<script lang="ts">
  import { invoke } from '@tauri-apps/api/core';
  import { onMount } from 'svelte';
  import type { FirstRun, Job } from '$lib/status';

  let info = $state<FirstRun | null>(null);
  let folder = $state('');
  let error = $state<string | null>(null);
  let starting = $state(false);

  onMount(async () => {
    info = await invoke<FirstRun>('first_run_info');
    folder = info.default_folder;
  });

  async function choose() {
    const picked = await invoke<string | null>('pick_folder', { current: folder });
    if (picked) folder = picked;
  }

  async function begin(job: Job, at: string) {
    error = null;
    starting = true;
    try {
      await invoke('begin_setup', { job, folder: at });
    } catch (err) {
      error = String(err);
      starting = false;
    }
  }
</script>

{#if info}
  <h1>Welcome to Istota</h1>
  {#if info.refusals.length}
    <section class="failure">
      <p>This Mac cannot run Istota:</p>
      <ul>
        {#each info.refusals as refusal (refusal)}
          <li>{refusal}</li>
        {/each}
      </ul>
    </section>
  {:else if !info.release}
    <section class="failure">
      <p>This copy of Istota carries no release to install. Download Istota again.</p>
    </section>
  {:else if info.offers.cli_install && info.offers.cli_workspace}
    <p>
      Istota is already installed on this Mac from the command line, with its files in
      <code>{info.offers.cli_workspace}</code>.
    </p>
    <p class="muted">
      Moving it stops that install and makes Istota again in the app from the folder's backups and
      the master key in your login Keychain. The old install is left stopped, not deleted.
    </p>
    <div class="actions">
      <button
        class="primary"
        disabled={starting}
        onclick={() => begin('move', info!.offers.cli_workspace!)}
      >
        Move your command-line Istota into the app
      </button>
    </div>
  {:else}
    {#if info.offers.standalone}
      <p class="notice">
        An older standalone Istota is on this Mac (<code>~/.istota</code>). Import it to keep its
        conversations, settings and files; it is left as it is.
      </p>
    {/if}
    <label class="folder">
      <span>Folder for Istota's files</span>
      <span class="row">
        <code>{folder}</code>
        <button onclick={choose} disabled={starting}>Choose…</button>
      </span>
    </label>
    <p class="muted">
      Istota keeps its files and backups here, visible in Finder. Not an iCloud folder, and not
      Documents, Desktop or Downloads.
    </p>
    <div class="actions">
      {#if info.offers.standalone}
        <button class="primary" disabled={starting} onclick={() => begin('import', folder)}>
          Import your standalone Istota
        </button>
        <button disabled={starting} onclick={() => begin('create', folder)}>
          Set up a new Istota instead
        </button>
      {:else}
        <button class="primary" disabled={starting} onclick={() => begin('create', folder)}>
          Set up Istota on this Mac
        </button>
      {/if}
    </div>
    <p class="muted small">Release {info.release}. Setup needs no network connection.</p>
  {/if}
  {#if error}
    <p class="notice">{error}</p>
  {/if}
{/if}

<style>
  .folder {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin: 16px 0 6px;
  }

  .row {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  ul {
    margin: 6px 0 0;
    padding-left: 18px;
  }

  .small {
    font-size: 12px;
    margin-top: 16px;
  }
</style>
