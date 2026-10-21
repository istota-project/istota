<script lang="ts">
  import { onMount } from 'svelte';
  import { getCredentialActivity, type CredentialActivityRow } from '$lib/api';
  import { Button, Card, Timestamp } from '@minot/ui';
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

<Card title="Activity" description="Recent credential actions.">
  {#snippet actions()}<Button variant="secondary" size="sm" disabled={busy} onclick={load}
      >Refresh</Button
    >{/snippet}
  {#if error}<p class="form-error" role="alert">{error}</p>{/if}
  {#if busy && !rows.length}<p class="caption">Loading activity…</p>
  {:else if !rows.length}<p>No credential activity yet.</p>
  {:else}
    <ul>
      {#each rows as row (row.id)}
        <li>
          <p>{row.action}{row.name ? ` · ${row.name}` : ''}</p>
          <p class="caption">
            <Timestamp value={row.at} format="datetime" /> · {row.actor}
          </p>
        </li>
      {/each}
    </ul>
  {/if}
</Card>

<style>
  ul {
    list-style: none;
    padding: 0;
    display: grid;
    gap: var(--space-2);
  }

  li {
    overflow-wrap: anywhere;
  }
</style>
