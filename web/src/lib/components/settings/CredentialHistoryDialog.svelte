<script lang="ts">
  import { onMount } from 'svelte';
  import {
    getCredentialHistory,
    restoreCredentialHistory,
    purgeCredentialHistory,
    type CredentialHistory,
    type StepUpProof,
  } from '$lib/api';
  import { Button, Modal } from '$lib/components/ui';
  import StepUpDialog from './StepUpDialog.svelte';
  let {
    name,
    onClose,
    onChanged = () => {},
  }: { name: string; onClose: () => void; onChanged?: () => void } = $props();
  let rows: CredentialHistory[] = $state([]);
  let error = $state('');
  let loading = $state(true);
  let action: { kind: 'history_restore' | 'history_purge'; id?: number } | null = $state(null);
  async function load() {
    try {
      rows = await getCredentialHistory(name);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }
  async function apply(proof: StepUpProof) {
    if (action?.kind === 'history_restore') await restoreCredentialHistory(action.id!, proof);
    else await purgeCredentialHistory(name, proof);
    onChanged();
    await load();
  }
  onMount(() => {
    void load();
  });
</script>

<Modal
  open={!action}
  title="History for {name}"
  onOpenChange={(open) => {
    if (!open && !action) onClose();
  }}
>
  {#if error}<p class="form-error" role="alert">{error}</p>{/if}
  {#if loading}<p>Loading history…</p>
  {:else if !rows.length}<p>No saved versions.</p>
  {:else}
    <p>Restoring replaces the current fields. Their current values are kept in history.</p>
    <ul>
      {#each rows as row (row.id)}
        <li>
          <p>{row.op} · {row.at} · {row.actor}</p>
          <p class="caption">{row.fields.join(', ')}</p>
          <Button
            variant="secondary"
            onclick={() => (action = { kind: 'history_restore', id: row.id })}>Restore</Button
          >
        </li>
      {/each}
    </ul>
  {/if}
  {#snippet footer()}
    <Button
      variant="danger"
      disabled={!rows.length}
      onclick={() => (action = { kind: 'history_purge' })}>Purge history</Button
    >
    <Button variant="ghost" onclick={onClose}>Close</Button>
  {/snippet}
</Modal>
{#if action}
  <StepUpDialog
    action={action.kind}
    {name}
    onConfirm={apply}
    onComplete={() => (action = null)}
    onCancel={() => (action = null)}
  />
{/if}
