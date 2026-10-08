<script lang="ts">
  import { onMount } from 'svelte';
  import { getCredentialActivity, type CredentialActivityRow } from '$lib/api';
  import { Button } from '$lib/components/ui';
  import SettingsCard from './SettingsCard.svelte';
  let rows: CredentialActivityRow[] = $state([]);
  let error = $state('');
  let busy = $state(false);
  async function load() {
    busy = true;
    error = '';
    try {
      rows = await getCredentialActivity();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
    }
  }
  onMount(() => {
    void load();
  });
</script>

<SettingsCard title="Activity" description="Recent credential actions.">
  {#snippet actions()}<Button variant="ghost" disabled={busy} onclick={load}>Refresh</Button
    >{/snippet}
  {#if error}<p class="form-error" role="alert">{error}</p>{/if}
  {#if busy && !rows.length}<p>Loading activity…</p>
  {:else if !rows.length}<p>No credential activity yet.</p>
  {:else}
    <ul>
      {#each rows as row (row.id)}
        <li>
          <p>{row.action}{row.name ? ` · ${row.name}` : ''}</p>
          <p class="caption">{row.at} · {row.actor}</p>
        </li>
      {/each}
    </ul>
  {/if}
</SettingsCard>
