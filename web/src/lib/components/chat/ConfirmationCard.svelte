<script lang="ts">
  import { Button } from '$lib/components/ui';

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
    margin-top: var(--space-2);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    flex-wrap: wrap;
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: var(--space-2) var(--space-3);
  }
  .confirm-label {
    font-size: var(--text-sm);
    color: var(--text-secondary);
  }
  .confirm-actions {
    display: flex;
    gap: var(--space-2);
  }
</style>
