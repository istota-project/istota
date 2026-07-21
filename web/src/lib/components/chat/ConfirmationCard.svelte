<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';

  let {
    onConfirm,
    onReject,
    busy = false,
  }: { onConfirm: () => void; onReject: () => void; busy?: boolean } = $props();
</script>

<div class="confirm-card">
  <span class="confirm-label">This action needs your confirmation.</span>
  <div class="confirm-actions">
    <Button variant="primary" size="sm" disabled={busy} onclick={onConfirm}>Confirm</Button>
    <Button variant="subtle" size="sm" disabled={busy} onclick={onReject}>Cancel</Button>
  </div>
</div>

<style>
  .confirm-card {
    margin-top: 0.5rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    flex-wrap: wrap;
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: 0.5rem 0.65rem;
  }
  .confirm-label {
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }
  .confirm-actions {
    display: flex;
    gap: 0.4rem;
  }
</style>
