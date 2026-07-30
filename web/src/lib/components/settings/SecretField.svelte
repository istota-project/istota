<script lang="ts">
  interface Props {
    label: string;
    type?: 'text' | 'email' | 'password' | 'url';
    configured: boolean;
    value: string;
    disabled?: boolean;
    onValueChange: (next: string) => void;
    onRequestClear?: () => void;
  }

  let {
    label,
    type = 'password',
    configured,
    value,
    disabled = false,
    onValueChange,
    onRequestClear,
  }: Props = $props();
</script>

<label class="secret-field">
  <span class="secret-label">{label}</span>
  <div class="secret-row">
    <input
      class="secret-input"
      {type}
      autocomplete="new-password"
      placeholder={configured ? '•••• stored — enter to replace' : 'Enter value'}
      {value}
      oninput={(e) => onValueChange((e.currentTarget as HTMLInputElement).value)}
      {disabled}
    />
    {#if configured && onRequestClear}
      <button
        class="secret-clear"
        title="Clear stored value"
        type="button"
        {disabled}
        onclick={onRequestClear}>×</button
      >
    {/if}
  </div>
</label>

<style>
  .secret-field {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    font-size: var(--text-sm);
  }

  .secret-label {
    color: var(--text-muted);
  }

  .secret-row {
    display: flex;
    gap: var(--space-2);
    align-items: center;
  }

  .secret-input {
    background: var(--surface-base);
    color: var(--text-primary);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2);
    font: inherit;
    font-size: var(--text-sm);
    width: 100%;
    max-width: 24rem;
    min-width: 0;
    box-sizing: border-box;
  }

  .secret-clear {
    background: transparent;
    border: none;
    color: var(--text-dim);
    cursor: pointer;
    padding: 0.1rem var(--space-2);
    border-radius: var(--radius-sm);
    font: inherit;
    font-size: var(--text-base);
    line-height: 1;
  }

  .secret-clear:hover:not(:disabled) {
    color: var(--status-danger-fg);
    background: var(--surface-raised);
  }

  .secret-clear:disabled {
    opacity: 0.3;
    cursor: not-allowed;
  }
</style>
